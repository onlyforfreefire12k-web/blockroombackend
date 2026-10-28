/**
 * Blockroom TV player - reference implementation (drop-in module).
 *
 * WHAT THIS IS
 * ------------
 * A self-contained, dependency-free ES module that turns a DOM element into
 * a real playing TV for one Blockroom.  It implements the exact media-state
 * contract the bot writes (see FIREBASE_SCHEMA.md in the bot repo):
 *
 *     rooms/{roomId}/media/current
 *
 * and performs REAL audio/video playback through the official YouTube IFrame
 * Player API - the only legitimate, browser-supported way to play YouTube
 * content.  A raw youtube.com URL or mp4 guess is never used.
 *
 * MEDIA FLOW (end to end)
 * -----------------------
 * Telegram /play /vplay -> bot searches YouTube (embeddable only)
 *   -> bot writes rooms/{roomId}/media/current
 *   -> THIS module's Firestore onSnapshot fires
 *   -> TV loads videoId into the YouTube IFrame player and plays it.
 *
 * DESIGN RULES (do not break these when integrating)
 * ---------------------------------------------------
 * 1. DISPLAY-ONLY: this module NEVER writes to Firestore.  Tab switches,
 *    minimizing, or closing the game cannot pause/stop media for other
 *    players - /skip and /stop in Telegram are the only controls.
 * 2. ONE ROOM ONLY: it subscribes to exactly one room's media/current.
 *    Two rooms never share state, so two TVs never interfere.
 * 3. HONEST STATES: loading / blocked-autoplay / unsupported-source errors
 *    are shown on the TV itself instead of silently failing.
 * 4. NO SECRETS: pass your web Firebase config and the bot API base URL in
 *    from your own hosting environment.  Never embed the bot token or the
 *    Firebase service account here - the browser only ever receives a
 *    short-lived custom token minted by the bot's /api/auth/telegram.
 *
 * The host game supplies:
 *   - container          DOM element to mount the TV in
 *   - roomId             the room this TV belongs to (from start_param)
 *   - telegramInitData   Telegram.WebApp.initData (identity for auth)
 *   - botApiBase         https://<your-render-service>.onrender.com
 *   - firebaseConfig     your web app's Firebase config (public values)
 *   - firestore          OPTIONAL: an already-initialized Firestore instance
 *
 * Usage:
 *   import { createBlockroomTV } from "./tv-player.js";
 *   const tv = await createBlockroomTV({ container, roomId, telegramInitData,
 *                                        botApiBase, firebaseConfig });
 *   // later: tv.destroy();
 */

const YT_API_SRC = "https://www.youtube.com/iframe_api";
const FIRESTORE_IMPORTS = "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";
const AUTH_IMPORTS = "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";

// Firestore media states written by the bot (media.py).
const ACTIVE_STATES = new Set(["playing", "buffering"]);

let ytApiPromise = null;
function loadYouTubeIframeApi() {
  if (window.YT && window.YT.Player) return Promise.resolve(window.YT);
  if (ytApiPromise) return ytApiPromise;
  ytApiPromise = new Promise((resolve) => {
    const previous = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      if (typeof previous === "function") previous();
      resolve(window.YT);
    };
    const script = document.createElement("script");
    script.src = YT_API_SRC;
    document.head.appendChild(script);
  });
  return ytApiPromise;
}

async function ensureFirebaseApps(firebaseConfig) {
  const [{ initializeApp, getApps }, { getAuth, signInWithCustomToken },
         firestore] = await Promise.all([
    import("https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js"),
    import(AUTH_IMPORTS),
    import(FIRESTORE_IMPORTS),
  ]);
  const app = getApps().length ? getApps()[0] : initializeApp(firebaseConfig);
  return {
    app,
    auth: getAuth(app),
    fs: firestore,
    signInWithCustomToken,
  };
}

async function exchangeInitDataForToken(botApiBase, initData) {
  const response = await fetch(botApiBase.replace(/\/$/, "") + "/api/auth/telegram", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ initData }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.ok) {
    throw new Error("TV auth failed: " + (payload.error || response.status));
  }
  return payload; // { ok, token, firebaseUid, user }
}

export async function createBlockroomTV(options) {
  const {
    container,
    roomId,
    telegramInitData,
    botApiBase,
    firebaseConfig,
    firestore: existingFirestore,
  } = options;
  if (!container || !roomId) {
    throw new Error("createBlockroomTV requires { container, roomId }");
  }

  const ui = buildUi(container);
  const tv = {
    roomId,
    destroy() { /* replaced at the end of setup */ },
  };
  let player = null;
  let currentVideoId = null;
  let unsubscribe = null;
  let retryTimer = null;
  let disposed = false;
  let soundUnlocked = false;

  // ---------------------------------------------------------------- UI ----
  function buildUi(root) {
    root.innerHTML = "";
    root.style.position = root.style.position || "relative";
    const frameHost = document.createElement("div");
    frameHost.className = "blockroom-tv-frame";
    frameHost.style.cssText =
      "position:absolute;inset:0;pointer-events:none;"; // TV is display-only
    const overlay = document.createElement("div");
    overlay.className = "blockroom-tv-overlay";
    overlay.style.cssText =
      "position:absolute;inset:0;display:none;flex-direction:column;" +
      "align-items:center;justify-content:center;text-align:center;" +
      "gap:10px;background:rgba(5,8,22,.78);color:#e8ecff;" +
      "font:600 15px/1.4 system-ui,sans-serif;padding:16px;z-index:2;" +
      "border-radius:inherit;cursor:pointer;";
    root.appendChild(frameHost);
    root.appendChild(overlay);
    return { frameHost, overlay };
  }

  function showOverlay(html, clickable) {
    ui.overlay.innerHTML = html;
    ui.overlay.style.display = "flex";
    ui.overlay.style.pointerEvents = clickable ? "auto" : "none";
    ui.overlay.style.cursor = clickable ? "pointer" : "default";
  }
  function hideOverlay() {
    ui.overlay.style.display = "none";
    ui.overlay.style.pointerEvents = "none";
  }

  function tvMessage(text) {
    showOverlay("<div>" + escapeHtml(text) + "</div>", false);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      }[ch]));
  }

  // ----------------------------------------------------------- YT glue ----
  async function ensurePlayer(videoId, autoplay) {
    const YT = await loadYouTubeIframeApi();
    if (player) {
      // Reuse the existing iframe player for every new video.
      if (autoplay) player.loadVideoById(videoId);
      else player.cueVideoById(videoId);
      return player;
    }
    return await new Promise((resolve) => {
      player = new YT.Player(ui.frameHost, {
        videoId: videoId || "",
        host: "https://www.youtube-nocookie.com",
        playerVars: {
          autoplay: autoplay ? 1 : 0,
          mute: 1,                       // start muted: browsers allow this
          controls: 0,                   // TV is display-only
          disablekb: 1,
          modestbranding: 1,
          rel: 0,
          playsinline: 1,
          iv_load_policy: 3,
          origin: window.location.origin,
        },
        events: {
          onReady: () => resolve(player),
          onStateChange: onPlayerState,
          onError: onPlayerError,
        },
      });
    });
  }

  function onPlayerState(event) {
    if (disposed) return;
    // YT states: -1 unstarted, 0 ended, 1 playing, 2 paused, 3 buffering, 5 cued
    const state = event.data;
    if (state === 1 || state === 3) {
      if (!soundUnlocked) {
        // Playing but muted: offer the one-tap gesture browsers require.
        showOverlay(
          "<div style='font-size:34px'>🔊</div>" +
          "<div>Tap to enable sound</div>", true);
      } else {
        hideOverlay();
      }
    } else if (state === 0) {
      // Ended.  Queue advancement belongs to the room owner (/skip) - the
      // TV never promotes media itself and never writes to Firebase.
      tvMessage("Ended. Waiting for the room owner…");
    }
  }

  function onPlayerError(event) {
    if (disposed) return;
    const code = event && event.data;
    const reasons = {
      2: "This video id is invalid.",
      5: "The browser could not play this video.",
      100: "This video was removed or is private.",
      101: "The uploader does not allow this video to play in other sites.",
      150: "The uploader does not allow this video to play in other sites.",
    };
    tvMessage("Cannot play: " + (reasons[code] || "unsupported source.") +
      " The room owner can /skip to the next one.");
    // Honest failure: the TV shows it; it does NOT pretend to play and does
    // not write anything back to Firebase.
  }

  // One tap on the overlay is the user gesture browsers require for sound.
  // It only unmutes THIS viewer's speaker - nothing is written to Firebase,
  // so other players are never interrupted by someone else's tab.
  ui.overlay.addEventListener("click", () => {
    if (!player) return;
    try {
      player.unMute();
      player.setVolume(100);
      if (typeof player.playVideo === "function") player.playVideo();
      soundUnlocked = true;
      hideOverlay();
    } catch (error) {
      tvMessage("Tap the TV again to enable sound.");
    }
  });

  // ------------------------------------------------------ media states ----
  function applyMediaState(state) {
    if (disposed) return;
    if (!state || !state.videoId ||
        (state.playbackState && !ACTIVE_STATES.has(state.playbackState))) {
      // stopped / missing -> idle TV (never writes anything back).
      stopPlayback("Nothing is on the TV. Send /play in Telegram.");
      return;
    }
    if (state.embeddable === false) {
      tvMessage("This video cannot be embedded. The room owner can /skip.");
      return;
    }
    if (state.playbackMethod && state.playbackMethod !== "youtube-iframe") {
      tvMessage("Unsupported source: " + state.playbackMethod);
      return;
    }
    const isNewVideo = state.videoId !== currentVideoId;
    if (!isNewVideo && state.playbackState === "buffering") return;

    currentVideoId = state.videoId;
    tvMessage("Loading: " + (state.title || state.videoId) + "…");

    ensurePlayer(state.videoId, true).then((readyPlayer) => {
      if (disposed || state.videoId !== currentVideoId) return;
      // Re-sync position for late joiners: the bot stamps startedAt.
      let offset = 0;
      if (state.startedAt) {
        const started = Date.parse(state.startedAt);
        if (!Number.isNaN(started)) {
          offset = Math.max(0, (Date.now() - started) / 1000);
          if (state.durationSeconds > 0) {
            offset = Math.min(offset, Math.max(0, state.durationSeconds - 5));
          }
        }
      }
      if (offset > 3 && readyPlayer.seekTo) readyPlayer.seekTo(offset, true);
    }).catch((error) => {
      console.error("[blockroom-tv] player init failed", error);
      tvMessage("The TV player failed to start. Reconnecting…");
      player = null;
      scheduleListenerRestart(4000);
    });
  }

  function stopPlayback(message) {
    currentVideoId = null;
    try {
      if (player && player.stopVideo) player.stopVideo();
    } catch (_) { /* already gone */ }
    tvMessage(message || "Nothing is on the TV.");
  }

  // ------------------------------------------------- Firestore listener ----
  async function startListener(firestore, fsApi) {
    const docRef = fsApi.doc(firestore, "rooms", roomId, "media", "current");
    // Live subscription - the TV reacts within ~a second of /play.
    unsubscribe = fsApi.onSnapshot(docRef, {
      includeMetadataChanges: false,
      next: (snapshot) => {
        if (disposed) return;
        applyMediaState(snapshot.exists ? snapshot.data() : null);
      },
      error: (error) => {
        if (disposed) return;
        console.error("[blockroom-tv] listener error", error);
        tvMessage("Lost the connection to the room. Reconnecting…");
        scheduleListenerRestart(3000);
      },
    });
  }

  function scheduleListenerRestart(delay) {
    if (disposed || retryTimer) return;
    retryTimer = setTimeout(async () => {
      retryTimer = null;
      try {
        if (unsubscribe) { unsubscribe(); unsubscribe = null; }
        await attachListener();
      } catch (error) {
        console.error("[blockroom-tv] reconnect failed", error);
        scheduleListenerRestart(6000);
      }
    }, delay);
  }

  async function attachListener() {
    const { firestore, fsApi } = tv._firebase;
    await startListener(firestore, fsApi);
  }

  // ------------------------------------------------------------- boot ----
  // 1) Firebase: reuse the host's Firestore when provided, else initialize.
  let firestore, fsApi, authApi;
  if (existingFirestore) {
    fsApi = await import(FIRESTORE_IMPORTS);
    firestore = existingFirestore;
  } else {
    if (!firebaseConfig) {
      throw new Error("Provide firebaseConfig (or an initialized firestore).");
    }
    const apps = await ensureFirebaseApps(firebaseConfig);
    firestore = apps.fs.getFirestore(apps.app);
    fsApi = apps.fs;
    authApi = apps;
  }

  // 2) Auth: exchange signed initData for a Firebase custom token, so
  //    Firestore Security Rules can trust this browser session.  Playback
  //    itself is read-only, but rules may still require an authenticated
  //    request; if auth fails we continue unauthenticated and let the rules
  //    decide (the TV only needs read access).
  if (authApi && telegramInitData) {
    try {
      const payload = await exchangeInitDataForToken(botApiBase, telegramInitData);
      await authApi.signInWithCustomToken(authApi.auth, payload.token);
    } catch (error) {
      console.warn("[blockroom-tv] auth skipped:", error.message);
    }
  }

  tv._firebase = { firestore, fsApi };

  // 3) Subscribe to this room's media state and start playing whatever is
  //    already current (late joiners hear the music in progress).
  stopPlayback("Connecting to the room…");
  await attachListener();

  tv.destroy = () => {
    disposed = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (unsubscribe) unsubscribe();
    try { if (player && player.destroy) player.destroy(); } catch (_) {}
    container.innerHTML = "";
  };

  return tv;
}

export default createBlockroomTV;
