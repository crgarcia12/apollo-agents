"use strict";

const socketProtocol = window.location.protocol === "https:" ? "wss" : "ws";
const LANDER_WS_URL = `${socketProtocol}://${window.location.host}/lander/ws`;

const MISSION_START_GET = "102:42:00";
const MISSION_END_GET = "102:45:57";
const MISSION_START_SECONDS = getToSeconds(MISSION_START_GET);
const MISSION_END_SECONDS = getToSeconds(MISSION_END_GET);
const MISSION_TIME_SCALE = 3;
const TELEMETRY_INTERVAL_SECONDS = 0.2;

const GRAVITY = 1.62;
const MAX_THRUST_ACCELERATION = 4.9;
const ROTATION_RATE_RADIANS = 1.05;
const FUEL_BURN_PER_SECOND = 0.68;
const LANDER_CENTER_TO_FEET_M = 11;
const LANDING_PAD_CENTER_X = 0;
const LANDING_PAD_HALF_WIDTH_M = 92;
const SAFE_VERTICAL_SPEED_MPS = 5;
const SAFE_HORIZONTAL_SPEED_MPS = 3;
const SAFE_ANGLE_DEGREES = 12;
const MAX_CORE_SETS = 7;
const PLAYER_ID_STORAGE_KEY = "apollo11_lander_player_id";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const dom = {
  canvas: document.getElementById("scene"),
  timelineFill: document.getElementById("timeline-fill"),
  timelineNeedle: document.getElementById("timeline-needle"),
  timelineMarkers: document.getElementById("timeline-markers"),
  getClock: document.getElementById("get-clock"),
  gamePhase: document.getElementById("game-phase"),
  overlay: document.getElementById("game-overlay"),
  overlayKicker: document.getElementById("overlay-kicker"),
  overlayTitle: document.getElementById("overlay-title"),
  overlayCopy: document.getElementById("overlay-copy"),
  startButton: document.getElementById("start-button"),
  restartButton: document.getElementById("restart-button"),
  soundButton: document.getElementById("sound-button"),
  soundIcon: document.getElementById("sound-icon"),
  soundLabel: document.getElementById("sound-label"),
  backgroundAudio: document.getElementById("background-audio"),
  alarmFlash: document.getElementById("alarm-flash"),
  alarmCode: document.getElementById("alarm-flash-code"),
  alarmNote: document.getElementById("alarm-flash-note"),
  gameStateBadge: document.getElementById("game-state-badge"),
  targetBearing: document.getElementById("target-bearing"),
  hudAlt: document.getElementById("hud-alt"),
  hudVspd: document.getElementById("hud-vspd"),
  hudHspd: document.getElementById("hud-hspd"),
  hudPitch: document.getElementById("hud-pitch"),
  hudFuel: document.getElementById("hud-fuel"),
  hudPvn: document.getElementById("hud-pvn"),
  fuelFill: document.getElementById("fuel-fill"),
  coreFill: document.getElementById("core-fill"),
  coreCount: document.getElementById("core-count"),
  coreMax: document.getElementById("core-max"),
  hudRadar: document.getElementById("hud-radar"),
  hudAlarm: document.getElementById("hud-alarm"),
  hudRestart: document.getElementById("hud-restart"),
  playerIdButton: document.getElementById("player-id-button"),
  gameIdButton: document.getElementById("game-id-button"),
  hudPlayerId: document.getElementById("hud-player-id"),
  hudGameId: document.getElementById("hud-game-id"),
  connectionStatus: document.getElementById("conn-status"),
  fabricStatus: document.getElementById("hud-fabric"),
  eventsSent: document.getElementById("hud-events-sent"),
  serverReceived: document.getElementById("hud-server-received"),
  eventLog: document.getElementById("event-log"),
};

const ctx = dom.canvas.getContext("2d");
const controls = {
  left: false,
  right: false,
  thrust: false,
};

const controlButtons = new Map(
  [...document.querySelectorAll("[data-control]")].map((button) => [
    button.dataset.control,
    button,
  ]),
);

const stars = Array.from({ length: 170 }, () => ({
  x: Math.random(),
  y: Math.random() * 0.76,
  radius: 0.25 + Math.random() * 1.25,
  phase: Math.random() * Math.PI * 2,
}));

let viewport = { width: 1100, height: 590, dpr: 1 };
let missionEvents = [];
let timelineMarkers = [];
let missionEventCursor = 0;
let gameSocket = null;
let socketReconnectTimer = null;
let attemptId = null;
const eventOutbox = new Map();
let gameState = "ready";
let lander = createInitialLander();
let missionSeconds = MISSION_START_SECONDS;
let gameElapsedSeconds = 0;
let activeAlarmCode = null;
let activeAlarmNote = "";
let guidanceProgram = "P64";
let guidanceVerb = 16;
let guidanceNoun = 68;
let program = "P64";
let verb = 16;
let noun = 68;
let coreSetsUsed = 4;
let restartLampUntil = 0;
let telemetryAccumulator = 0;
let telemetrySequence = 0;
let eventsSent = 0;
let fuelEmptyReported = false;
let lastFrameTime = performance.now();
let trailAccumulator = 0;
let trail = [];
let particles = [];
let audioEnabled = true;
let audioErrorReported = false;
const playerId = getOrCreatePlayerId();

function createInitialLander() {
  return {
    x: -420,
    y: 390,
    vx: 10,
    vy: -4.2,
    angle: 0,
    fuel: 100,
    throttle: 0,
  };
}

function clamp(value, lower, upper) {
  return Math.max(lower, Math.min(upper, value));
}

function getToSeconds(getValue) {
  const [hours, minutes, seconds] = getValue.split(":").map(Number);
  return hours * 3600 + minutes * 60 + seconds;
}

function secondsToGet(value) {
  const wholeSeconds = Math.max(0, Math.floor(value));
  const hours = Math.floor(wholeSeconds / 3600);
  const minutes = Math.floor((wholeSeconds % 3600) / 60);
  const seconds = wholeSeconds % 60;
  return `${String(hours).padStart(3, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function radiansToDegrees(value) {
  return value * 180 / Math.PI;
}

function formatSigned(value, decimals = 1) {
  const prefix = value >= 0 ? "+" : "";
  return `${prefix}${value.toFixed(decimals)}`;
}

function createMessageId() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (character) => {
    const random = Math.floor(Math.random() * 16);
    const value = character === "x" ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

function getOrCreatePlayerId() {
  try {
    const storedPlayerId = window.localStorage.getItem(PLAYER_ID_STORAGE_KEY);
    if (storedPlayerId && UUID_PATTERN.test(storedPlayerId)) {
      return storedPlayerId;
    }

    const newPlayerId = createMessageId();
    window.localStorage.setItem(PLAYER_ID_STORAGE_KEY, newPlayerId);
    return newPlayerId;
  } catch (error) {
    console.warn("Player ID could not be persisted in browser storage", error);
    return createMessageId();
  }
}

function shortIdentifier(identifier) {
  return identifier ? identifier.split("-", 1)[0].toUpperCase() : "NOT STARTED";
}

async function copyIdentifier(identifier, label) {
  if (!identifier) {
    return;
  }

  try {
    await navigator.clipboard.writeText(identifier);
    addLog(
      "system",
      secondsToGet(missionSeconds),
      `${label} COPIED - ${identifier}`,
    );
  } catch (error) {
    console.warn(`${label} could not be copied`, error);
    addLog(
      "system",
      secondsToGet(missionSeconds),
      `${label} COPY FAILED`,
    );
  }
}

function localTerrainHeight(x) {
  if (Math.abs(x - LANDING_PAD_CENTER_X) <= LANDING_PAD_HALF_WIDTH_M + 18) {
    return 14;
  }

  const ridge =
    14 +
    Math.sin(x * 0.011) * 8 +
    Math.sin(x * 0.027 + 1.4) * 5 +
    Math.sin(x * 0.004 - 0.6) * 10;
  const craterOne = -18 * Math.exp(-Math.pow((x + 370) / 72, 2));
  const craterTwo = -13 * Math.exp(-Math.pow((x - 310) / 95, 2));
  const craterThree = -10 * Math.exp(-Math.pow((x + 720) / 58, 2));
  return ridge + craterOne + craterTwo + craterThree;
}

function altitudeAboveGround() {
  return Math.max(
    0,
    lander.y - localTerrainHeight(lander.x) - LANDER_CENTER_TO_FEET_M,
  );
}

function landingTargetDistance() {
  return LANDING_PAD_CENTER_X - lander.x;
}

function resetMissionState() {
  lander = createInitialLander();
  gameState = "ready";
  missionSeconds = MISSION_START_SECONDS;
  gameElapsedSeconds = 0;
  activeAlarmCode = null;
  activeAlarmNote = "";
  guidanceProgram = "P64";
  guidanceVerb = 16;
  guidanceNoun = 68;
  program = "P64";
  verb = 16;
  noun = 68;
  coreSetsUsed = 4;
  restartLampUntil = 0;
  telemetryAccumulator = 0;
  telemetrySequence = 0;
  dom.gamePhase.textContent = "P64 · APPROACH";
  dom.timelineMarkers
    .querySelectorAll(".actual-landing")
    .forEach((marker) => marker.remove());
  missionEventCursor = missionEvents.findIndex((event) => event.t >= MISSION_START_SECONDS);
  if (missionEventCursor < 0) {
    missionEventCursor = missionEvents.length;
  }
  fuelEmptyReported = false;
  trailAccumulator = 0;
  trail = [{ x: lander.x, y: lander.y }];
  particles = [];
  clearControls();
  hideAlarm();
  updateTimeline();
  renderHud();
}

function startGame() {
  if (gameState === "flying") {
    return;
  }

  resetMissionState();
  attemptId = createMessageId();
  gameState = "flying";
  playBackgroundAudio();
  dom.overlay.classList.add("hidden");
  setGameStateBadge();
  addLog("system", MISSION_START_GET, "MANUAL CONTROL ACCEPTED - guide Eagle to the landing pad");
  sendGameEvent("game_start", "Player took manual control of Eagle");
}

function restartGame() {
  if (gameState === "flying") {
    sendGameEvent("mission_restart", "Player restarted the landing attempt");
  }
  resetMissionState();
  startGame();
}

function endGame(outcome, title, copy, eventType, note, touchdown) {
  gameState = outcome;
  lander.throttle = 0;
  clearControls();
  setGameStateBadge();
  addActualLandingMarker(outcome);
  sendGameEvent(eventType, note, touchdown);

  dom.overlayKicker.textContent =
    outcome === "landed" ? "HOUSTON, TRANQUILITY BASE HERE" : "MISSION FAILURE";
  dom.overlayTitle.textContent = title;
  dom.overlayCopy.textContent = copy;
  dom.startButton.textContent = "FLY AGAIN";
  dom.overlay.classList.remove("hidden");
}

function updateSoundButton() {
  dom.soundButton.classList.toggle("active", audioEnabled);
  dom.soundButton.setAttribute("aria-pressed", String(audioEnabled));
  dom.soundButton.setAttribute(
    "aria-label",
    audioEnabled ? "Mute background audio" : "Play background audio",
  );
  dom.soundIcon.textContent = audioEnabled ? "♫" : "♩";
  dom.soundLabel.textContent = audioEnabled ? "SOUND ON" : "SOUND OFF";
}

function playBackgroundAudio() {
  if (!audioEnabled || !dom.backgroundAudio.paused) {
    return;
  }

  const playResult = dom.backgroundAudio.play();
  if (playResult) {
    playResult.catch((error) => {
      if (!audioErrorReported) {
        audioErrorReported = true;
        console.warn("Background audio could not start", error);
      }
    });
  }
}

function toggleBackgroundAudio() {
  audioEnabled = !audioEnabled;
  if (audioEnabled) {
    playBackgroundAudio();
  } else {
    dom.backgroundAudio.pause();
  }
  updateSoundButton();
}

function evaluateTouchdown() {
  const surface = localTerrainHeight(lander.x);
  const verticalSpeed = Math.abs(lander.vy);
  const horizontalSpeed = Math.abs(lander.vx);
  const angleDegrees = Math.abs(radiansToDegrees(lander.angle));
  const onLandingPad =
    Math.abs(lander.x - LANDING_PAD_CENTER_X) <= LANDING_PAD_HALF_WIDTH_M;

  lander.y = surface + LANDER_CENTER_TO_FEET_M;
  lander.vx = 0;
  lander.vy = 0;
  lander.throttle = 0;

  const touchdown = {
    verticalSpeed,
    horizontalSpeed,
    angleDegrees,
  };

  if (
    onLandingPad &&
    verticalSpeed <= SAFE_VERTICAL_SPEED_MPS &&
    horizontalSpeed <= SAFE_HORIZONTAL_SPEED_MPS &&
    angleDegrees <= SAFE_ANGLE_DEGREES
  ) {
    lander.angle = 0;
    createLandingDust();
    endGame(
      "landed",
      "THE EAGLE HAS LANDED",
      `Safe touchdown at ${secondsToGet(missionSeconds)} with ${verticalSpeed.toFixed(1)} m/s vertical speed, ${horizontalSpeed.toFixed(1)} m/s horizontal speed, and ${angleDegrees.toFixed(1)} degrees pitch.`,
      "touchdown",
      "Safe player-controlled touchdown inside the landing zone",
      touchdown,
    );
    return;
  }

  lander.angle = 0;
  createCrashParticles();
  const failures = [];
  if (!onLandingPad) failures.push("outside the landing pad");
  if (verticalSpeed > SAFE_VERTICAL_SPEED_MPS) failures.push("vertical speed too high");
  if (horizontalSpeed > SAFE_HORIZONTAL_SPEED_MPS) failures.push("horizontal speed too high");
  if (angleDegrees > SAFE_ANGLE_DEGREES) failures.push("lander not upright");

  endGame(
    "crashed",
    "HARD LANDING",
    `Eagle was ${failures.join(", ")}. Touchdown: ${verticalSpeed.toFixed(1)} m/s vertical, ${horizontalSpeed.toFixed(1)} m/s horizontal, ${angleDegrees.toFixed(1)} degrees pitch.`,
    "crash",
    `Player landing failed: ${failures.join(", ")}`,
    touchdown,
  );
}

function updatePhysics(deltaSeconds) {
  if (controls.left !== controls.right) {
    const direction = controls.left ? -1 : 1;
    lander.angle += direction * ROTATION_RATE_RADIANS * deltaSeconds;
  }
  lander.angle = clamp(lander.angle, -Math.PI * 0.42, Math.PI * 0.42);

  const engineActive = controls.thrust && lander.fuel > 0;
  lander.throttle = engineActive ? 100 : 0;

  if (engineActive) {
    const acceleration = MAX_THRUST_ACCELERATION;
    lander.vx += Math.sin(lander.angle) * acceleration * deltaSeconds;
    lander.vy += Math.cos(lander.angle) * acceleration * deltaSeconds;
    lander.fuel = Math.max(0, lander.fuel - FUEL_BURN_PER_SECOND * deltaSeconds);
  }

  lander.vy -= GRAVITY * deltaSeconds;
  lander.x += lander.vx * deltaSeconds;
  lander.y += lander.vy * deltaSeconds;

  if (lander.fuel === 0 && !fuelEmptyReported) {
    fuelEmptyReported = true;
    addLog("system", secondsToGet(missionSeconds), "DESCENT STAGE FUEL DEPLETED");
    sendGameEvent("fuel_depleted", "Descent stage fuel reached zero");
  }

  const feetHeight = lander.y - LANDER_CENTER_TO_FEET_M;
  if (feetHeight <= localTerrainHeight(lander.x) && lander.vy <= 0) {
    evaluateTouchdown();
  }

  trailAccumulator += deltaSeconds;
  if (trailAccumulator >= 0.12) {
    trailAccumulator = 0;
    trail.push({ x: lander.x, y: lander.y });
    if (trail.length > 420) {
      trail.shift();
    }
  }
}

function updateMission(deltaSeconds) {
  const previousMissionSeconds = missionSeconds;
  gameElapsedSeconds += deltaSeconds;
  missionSeconds =
    MISSION_START_SECONDS + gameElapsedSeconds * MISSION_TIME_SCALE;

  const missionDelta = missionSeconds - previousMissionSeconds;
  if (!activeAlarmCode) {
    coreSetsUsed = Math.min(MAX_CORE_SETS - 0.05, coreSetsUsed + missionDelta * 0.12);
  }

  while (
    missionEventCursor < missionEvents.length &&
    missionEvents[missionEventCursor].t <= missionSeconds
  ) {
    const event = missionEvents[missionEventCursor];
    if (event.t >= previousMissionSeconds) {
      applyMissionEvent(event);
    }
    missionEventCursor += 1;
  }
}

function applyMissionEvent(event) {
  if (event.type === "program") {
    guidanceProgram = event.program || guidanceProgram;
    guidanceVerb = event.verb ?? guidanceVerb;
    guidanceNoun = event.noun ?? guidanceNoun;
    program = guidanceProgram;
    verb = guidanceVerb;
    noun = guidanceNoun;
    dom.gamePhase.textContent = `${program} · ${program === "P66" ? "MANUAL LANDING" : "APPROACH"}`;
  } else if (event.type === "alarm") {
    program = event.program || guidanceProgram;
    verb = event.verb ?? 5;
    noun = event.noun ?? 9;
    activeAlarmCode = String(event.code);
    activeAlarmNote = event.note || "";
    coreSetsUsed = MAX_CORE_SETS;
    showAlarm(activeAlarmCode, activeAlarmNote);
  } else if (event.type === "restart") {
    program = guidanceProgram;
    verb = guidanceVerb;
    noun = guidanceNoun;
    activeAlarmCode = null;
    activeAlarmNote = "";
    coreSetsUsed = 1.4;
    restartLampUntil = gameElapsedSeconds + 2.5;
    hideAlarm();
  }

  const eventCode = event.code ? ` [${event.code}]` : "";
  addLog(
    event.type,
    event.get,
    `${event.type.toUpperCase()}${eventCode} - ${event.note || ""}`,
  );
  sendGameEvent(
    event.type,
    event.note || "",
    null,
    event.code ? String(event.code) : null,
  );
}

function showAlarm(code, note) {
  dom.alarmCode.textContent = code;
  dom.alarmNote.textContent = note;
  dom.alarmFlash.classList.remove("hidden");
}

function hideAlarm() {
  dom.alarmFlash.classList.add("hidden");
}

function clearControls() {
  for (const control of Object.keys(controls)) {
    controls[control] = false;
    controlButtons.get(control)?.classList.remove("active");
  }
}

function setControl(control, active) {
  if (!(control in controls)) {
    return;
  }
  controls[control] = active && gameState === "flying";
  controlButtons.get(control)?.classList.toggle("active", controls[control]);
}

function addLog(className, getValue, text) {
  const row = document.createElement("div");
  row.className = className;
  row.textContent = `${getValue}  ${text}`;
  dom.eventLog.prepend(row);
  while (dom.eventLog.childElementCount > 80) {
    dom.eventLog.lastElementChild.remove();
  }
}

async function loadMissionTimeline() {
  const response = await fetch("/data/mission_timeline.json");
  if (!response.ok) {
    throw new Error(`Mission timeline request failed with ${response.status}`);
  }

  const data = await response.json();
  missionEvents = data.events
    .map((event) => ({ ...event, t: getToSeconds(event.get) }))
    .filter((event) => event.t >= MISSION_START_SECONDS)
    .sort((left, right) => left.t - right.t);

  dom.timelineMarkers.replaceChildren();
  timelineMarkers = missionEvents.map((event) => {
    const marker = document.createElement("div");
    const percentage =
      ((event.t - MISSION_START_SECONDS) /
        (MISSION_END_SECONDS - MISSION_START_SECONDS)) *
      100;
    marker.className = `tl-marker type-${event.type}`;
    marker.style.left = `${clamp(percentage, 0, 100)}%`;
    marker.title = `${event.get} - ${event.type.toUpperCase()}${event.code ? ` [${event.code}]` : ""}: ${event.note || ""}`;
    dom.timelineMarkers.appendChild(marker);
    return { event, marker };
  });
}

function updateTimeline() {
  const percentage =
    ((missionSeconds - MISSION_START_SECONDS) /
      (MISSION_END_SECONDS - MISSION_START_SECONDS)) *
    100;
  const clampedPercentage = clamp(percentage, 0, 100);
  dom.timelineFill.style.width = `${clampedPercentage}%`;
  dom.timelineNeedle.style.left = `${clampedPercentage}%`;
  dom.getClock.textContent = secondsToGet(missionSeconds);

  for (const item of timelineMarkers) {
    item.marker.classList.toggle("passed", missionSeconds >= item.event.t);
  }
}

function addActualLandingMarker(outcome) {
  const percentage =
    ((missionSeconds - MISSION_START_SECONDS) /
      (MISSION_END_SECONDS - MISSION_START_SECONDS)) *
    100;
  const marker = document.createElement("div");
  marker.className = `tl-marker actual-landing ${outcome}`;
  marker.style.left = `${clamp(percentage, 0, 100)}%`;
  marker.title = `${secondsToGet(missionSeconds)} - PLAYER ${outcome.toUpperCase()}`;
  dom.timelineMarkers.appendChild(marker);
}

function setGameStateBadge() {
  dom.gameStateBadge.className = `status-badge ${gameState}`;
  dom.gameStateBadge.textContent = gameState.toUpperCase();
}

function renderHud() {
  const altitude = altitudeAboveGround();
  const verticalDown = -lander.vy;
  const targetDistance = landingTargetDistance();
  const pitchDegrees = radiansToDegrees(lander.angle);
  const restartActive = gameElapsedSeconds < restartLampUntil;

  dom.hudAlt.textContent = `${altitude.toFixed(1)} m`;
  dom.hudVspd.textContent =
    verticalDown >= 0
      ? `${verticalDown.toFixed(1)} m/s DOWN`
      : `${Math.abs(verticalDown).toFixed(1)} m/s UP`;
  dom.hudHspd.textContent = `${formatSigned(lander.vx)} m/s`;
  dom.hudPitch.textContent = `${formatSigned(pitchDegrees)}°`;
  dom.hudFuel.textContent = `${lander.fuel.toFixed(1)}%`;
  dom.hudPvn.textContent = `${program} / V${String(verb).padStart(2, "0")} / N${String(noun).padStart(2, "0")}`;
  dom.fuelFill.style.width = `${lander.fuel}%`;
  dom.coreFill.style.width = `${(coreSetsUsed / MAX_CORE_SETS) * 100}%`;
  dom.coreCount.textContent = String(Math.round(coreSetsUsed));
  dom.coreMax.textContent = String(MAX_CORE_SETS);
  dom.hudAlarm.textContent = activeAlarmCode || "—";
  dom.hudAlarm.classList.toggle("warning", Boolean(activeAlarmCode));
  dom.hudRestart.textContent = restartActive ? "ACTIVE" : "ARMED";
  dom.hudRestart.classList.toggle("warning", restartActive);
  dom.targetBearing.textContent =
    Math.abs(targetDistance) < LANDING_PAD_HALF_WIDTH_M
      ? "TARGET BELOW"
      : `TARGET ${Math.abs(targetDistance).toFixed(0)} m ${targetDistance > 0 ? "→" : "←"}`;
  dom.eventsSent.textContent = String(eventsSent);
  dom.hudPlayerId.textContent = shortIdentifier(playerId);
  dom.playerIdButton.title = `Copy Player ID: ${playerId}`;
  dom.hudGameId.textContent = shortIdentifier(attemptId);
  dom.gameIdButton.disabled = !attemptId;
  dom.gameIdButton.title = attemptId
    ? `Copy Game ID: ${attemptId}`
    : "Start a landing to create a Game ID";
  setGameStateBadge();
}

function buildTelemetryPayload(kind, eventType, note = "", code = null, touchdown = null) {
  const payload = {
    kind,
    player_id: playerId,
    attempt_id: attemptId,
    sequence: telemetrySequence++,
    client_event_time: new Date().toISOString(),
    mission_get: secondsToGet(missionSeconds),
    sim_get_seconds: Number(missionSeconds.toFixed(2)),
    game_elapsed_s: Number(gameElapsedSeconds.toFixed(3)),
    event_type: eventType,
    game_state: gameState,
    program,
    verb,
    noun,
    code,
    active_alarm_code: activeAlarmCode,
    note,
    prog_alarm: Boolean(activeAlarmCode),
    restart_lamp: gameElapsedSeconds < restartLampUntil,
    radar_auto_slew: true,
    core_sets_used: Math.round(coreSetsUsed),
    max_core_sets: MAX_CORE_SETS,
    lander_x_m: Number(lander.x.toFixed(3)),
    lander_altitude_m: Number(altitudeAboveGround().toFixed(3)),
    lander_vertical_speed_mps: Number((-lander.vy).toFixed(3)),
    lander_horizontal_speed_mps: Number(lander.vx.toFixed(3)),
    lander_rotation_deg: Number(radiansToDegrees(lander.angle).toFixed(3)),
    lander_throttle_pct: lander.throttle,
    lander_fuel_pct: Number(lander.fuel.toFixed(3)),
    landing_target_distance_m: Number(landingTargetDistance().toFixed(3)),
    control_left: controls.left,
    control_right: controls.right,
    control_thrust: controls.thrust && lander.fuel > 0,
  };

  if (touchdown) {
    payload.touchdown_vertical_speed_mps = Number(touchdown.verticalSpeed.toFixed(3));
    payload.touchdown_horizontal_speed_mps = Number(touchdown.horizontalSpeed.toFixed(3));
    payload.touchdown_angle_deg = Number(touchdown.angleDegrees.toFixed(3));
  }

  return payload;
}

function sendPayload(payload) {
  if (!gameSocket || gameSocket.readyState !== WebSocket.OPEN) {
    return false;
  }
  gameSocket.send(JSON.stringify(payload));
  eventsSent += 1;
  return true;
}

function sendGameTelemetry() {
  sendPayload(
    buildTelemetryPayload(
      "lander_game_telemetry",
      "game_telemetry",
      "Player-controlled lunar module physics sample",
    ),
  );
}

function sendGameEvent(eventType, note, touchdown = null, code = null) {
  const payload = buildTelemetryPayload(
    "lander_game_event",
    eventType,
    note,
    code,
    touchdown,
  );
  payload.message_id = createMessageId();
  eventOutbox.set(payload.message_id, payload);
  sendPayload(payload);
}

function fabricStatusLabel(status) {
  if (status.eventhousePublishing && status.eventstreamPublishing) {
    return "EVENTSTREAM + EVENTHOUSE";
  }
  if (status.eventhousePublishing) {
    return "EVENTHOUSE LIVE";
  }
  if (status.eventstreamPublishing) {
    return "EVENTSTREAM LIVE";
  }
  return status.fabricPublishing ? "FABRIC LIVE" : "LOCAL NDJSON";
}

function flushEventOutbox() {
  for (const payload of eventOutbox.values()) {
    sendPayload(payload);
  }
}

async function connectGameSocket() {
  clearTimeout(socketReconnectTimer);
  dom.connectionStatus.textContent = "CONNECTING";
  dom.connectionStatus.classList.remove("warning");

  const sessionResponse = await fetch("/lander/session", { cache: "no-store" });
  if (!sessionResponse.ok) {
    throw new Error(`Lander session request failed with ${sessionResponse.status}`);
  }
  const session = await sessionResponse.json();
  gameSocket = new WebSocket(
    `${LANDER_WS_URL}?token=${encodeURIComponent(session.token)}`,
  );

  gameSocket.addEventListener("open", () => {
    dom.connectionStatus.textContent = "CONNECTED";
    dom.connectionStatus.style.color = "#56e67a";
  });

  gameSocket.addEventListener("message", (message) => {
    const payload = JSON.parse(message.data);
    if (payload.kind === "lander_session") {
      dom.fabricStatus.textContent = fabricStatusLabel(payload);
      dom.fabricStatus.style.color = payload.fabricPublishing ? "#56e67a" : "#ffc94c";
      flushEventOutbox();
    } else if (payload.kind === "lander_ack") {
      eventOutbox.delete(payload.message_id);
    } else if (payload.kind === "lander_error") {
      addLog("system", secondsToGet(missionSeconds), `SERVER REJECTED TELEMETRY - ${payload.message}`);
    }
  });

  gameSocket.addEventListener("close", () => {
    dom.connectionStatus.textContent = "RECONNECTING";
    dom.connectionStatus.style.color = "#ff5e61";
    socketReconnectTimer = setTimeout(() => {
      connectGameSocket().catch(scheduleSocketReconnect);
    }, 1500);
  });

  gameSocket.addEventListener("error", () => {
    gameSocket.close();
  });
}

function scheduleSocketReconnect(error) {
  console.warn("Lander socket connection failed", error);
  dom.connectionStatus.textContent = "RECONNECTING";
  dom.connectionStatus.style.color = "#ff5e61";
  clearTimeout(socketReconnectTimer);
  socketReconnectTimer = setTimeout(() => {
    connectGameSocket().catch(scheduleSocketReconnect);
  }, 1500);
}

async function pollHealth() {
  try {
    const response = await fetch("/healthz", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Health request failed with ${response.status}`);
    }
    const health = await response.json();
    dom.fabricStatus.textContent = fabricStatusLabel(health);
    dom.fabricStatus.style.color = health.fabricPublishing ? "#56e67a" : "#ffc94c";
    dom.serverReceived.textContent = String(health.landerMessagesReceived ?? 0);
  } catch (error) {
    dom.fabricStatus.textContent = "UNAVAILABLE";
    dom.fabricStatus.style.color = "#ff5e61";
  }
}

function createCrashParticles() {
  particles = Array.from({ length: 58 }, () => ({
    x: lander.x,
    y: lander.y,
    vx: lander.vx * 0.25 + (Math.random() - 0.5) * 24,
    vy: Math.random() * 18 + 3,
    life: 1.2 + Math.random() * 1.8,
    size: 1.5 + Math.random() * 4,
    color: Math.random() > 0.45 ? "#ffb13b" : "#aab6c7",
  }));
}

function createLandingDust() {
  particles = Array.from({ length: 34 }, () => ({
    x: lander.x + (Math.random() - 0.5) * 24,
    y: localTerrainHeight(lander.x) + Math.random() * 3,
    vx: (Math.random() - 0.5) * 20,
    vy: Math.random() * 4 + 1,
    life: 1.4 + Math.random() * 1.5,
    size: 2 + Math.random() * 3,
    color: "#a8a59d",
  }));
}

function updateParticles(deltaSeconds) {
  for (const particle of particles) {
    particle.life -= deltaSeconds;
    particle.vy -= GRAVITY * deltaSeconds;
    particle.x += particle.vx * deltaSeconds;
    particle.y += particle.vy * deltaSeconds;
  }
  particles = particles.filter((particle) => particle.life > 0);
}

function resizeCanvas() {
  const rectangle = dom.canvas.getBoundingClientRect();
  const width = Math.max(320, rectangle.width);
  const height = width * 590 / 1100;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  dom.canvas.width = Math.round(width * dpr);
  dom.canvas.height = Math.round(height * dpr);
  viewport = { width, height, dpr };
}

function createCamera() {
  const targetDistance = Math.abs(landingTargetDistance());
  const horizontalSpan = clamp(targetDistance * 1.4 + 410, 520, 1750);
  const midpoint = (lander.x + LANDING_PAD_CENTER_X) / 2;
  let left = midpoint - horizontalSpan / 2;
  let right = midpoint + horizontalSpan / 2;

  if (lander.x < left + 80) {
    left = lander.x - 80;
    right = left + horizontalSpan;
  } else if (lander.x > right - 80) {
    right = lander.x + 80;
    left = right - horizontalSpan;
  }

  const top = Math.max(170, lander.y * 1.16);
  const bottom = -25;
  const usableTop = 28;
  const usableBottom = viewport.height - 20;

  return {
    left,
    right,
    top,
    bottom,
    x(value) {
      return ((value - left) / (right - left)) * viewport.width;
    },
    y(value) {
      return usableTop + ((top - value) / (top - bottom)) * (usableBottom - usableTop);
    },
  };
}

function drawArrow(fromX, fromY, toX, toY, color) {
  const angle = Math.atan2(toY - fromY, toX - fromX);
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(fromX, fromY);
  ctx.lineTo(toX, toY);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(toX, toY);
  ctx.lineTo(toX - Math.cos(angle - 0.55) * 7, toY - Math.sin(angle - 0.55) * 7);
  ctx.lineTo(toX - Math.cos(angle + 0.55) * 7, toY - Math.sin(angle + 0.55) * 7);
  ctx.closePath();
  ctx.fill();
}

function drawBackground(camera, now) {
  const gradient = ctx.createLinearGradient(0, 0, 0, viewport.height);
  gradient.addColorStop(0, "#000105");
  gradient.addColorStop(0.62, "#050a13");
  gradient.addColorStop(1, "#111927");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, viewport.width, viewport.height);

  for (const star of stars) {
    const alpha = 0.35 + Math.abs(Math.sin(now * 0.0008 + star.phase)) * 0.55;
    ctx.fillStyle = `rgba(220,232,255,${alpha.toFixed(2)})`;
    ctx.beginPath();
    ctx.arc(
      star.x * viewport.width,
      star.y * viewport.height,
      star.radius,
      0,
      Math.PI * 2,
    );
    ctx.fill();
  }

  const earthX = viewport.width * 0.84;
  const earthY = viewport.height * 0.15;
  const earthRadius = clamp(viewport.width * 0.018, 10, 20);
  const earthGradient = ctx.createRadialGradient(
    earthX - earthRadius * 0.35,
    earthY - earthRadius * 0.35,
    1,
    earthX,
    earthY,
    earthRadius,
  );
  earthGradient.addColorStop(0, "#e6fbff");
  earthGradient.addColorStop(0.32, "#4bb8ed");
  earthGradient.addColorStop(0.68, "#1d5aa2");
  earthGradient.addColorStop(1, "#07152d");
  ctx.fillStyle = earthGradient;
  ctx.beginPath();
  ctx.arc(earthX, earthY, earthRadius, 0, Math.PI * 2);
  ctx.fill();

  const gridStep = camera.top > 700 ? 200 : camera.top > 300 ? 100 : 50;
  ctx.font = "9px Consolas, monospace";
  ctx.textAlign = "left";
  for (let altitude = 0; altitude < camera.top; altitude += gridStep) {
    const y = camera.y(altitude);
    ctx.strokeStyle = "rgba(93,123,163,0.11)";
    ctx.setLineDash([4, 7]);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(viewport.width, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(130,151,179,0.55)";
    ctx.fillText(`${altitude} m`, 8, y - 4);
  }
}

function drawTerrain(camera) {
  const sampleCount = Math.ceil(viewport.width / 5);
  const points = [];
  for (let index = 0; index <= sampleCount; index += 1) {
    const screenX = index / sampleCount * viewport.width;
    const worldX = camera.left + index / sampleCount * (camera.right - camera.left);
    points.push({
      x: screenX,
      y: camera.y(localTerrainHeight(worldX)),
    });
  }

  const surfaceGradient = ctx.createLinearGradient(0, camera.y(0), 0, viewport.height);
  surfaceGradient.addColorStop(0, "#a4a39e");
  surfaceGradient.addColorStop(0.12, "#777873");
  surfaceGradient.addColorStop(1, "#30343a");
  ctx.fillStyle = surfaceGradient;
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (const point of points.slice(1)) {
    ctx.lineTo(point.x, point.y);
  }
  ctx.lineTo(viewport.width, viewport.height);
  ctx.lineTo(0, viewport.height);
  ctx.closePath();
  ctx.fill();

  ctx.strokeStyle = "#d0cec4";
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (const point of points.slice(1)) {
    ctx.lineTo(point.x, point.y);
  }
  ctx.stroke();

  drawCrater(camera, -370, 70);
  drawCrater(camera, 310, 92);
  drawCrater(camera, -720, 55);
  drawLandingPad(camera);
}

function drawCrater(camera, centerX, radiusM) {
  if (centerX + radiusM < camera.left || centerX - radiusM > camera.right) {
    return;
  }
  const centerScreenX = camera.x(centerX);
  const radiusPixels = Math.abs(camera.x(centerX + radiusM) - centerScreenX);
  const centerScreenY = camera.y(localTerrainHeight(centerX)) + 3;
  ctx.fillStyle = "rgba(45,48,52,0.55)";
  ctx.beginPath();
  ctx.ellipse(
    centerScreenX,
    centerScreenY,
    Math.max(6, radiusPixels),
    Math.max(2, radiusPixels * 0.18),
    0,
    0,
    Math.PI * 2,
  );
  ctx.fill();
}

function drawLandingPad(camera) {
  const padY = localTerrainHeight(LANDING_PAD_CENTER_X);
  const left = camera.x(LANDING_PAD_CENTER_X - LANDING_PAD_HALF_WIDTH_M);
  const right = camera.x(LANDING_PAD_CENTER_X + LANDING_PAD_HALF_WIDTH_M);
  const y = camera.y(padY);

  ctx.strokeStyle = "#ffd258";
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.moveTo(left, y);
  ctx.lineTo(right, y);
  ctx.stroke();

  ctx.fillStyle = "#ffd258";
  for (let index = 0; index <= 6; index += 1) {
    const lightX = left + (right - left) * index / 6;
    ctx.beginPath();
    ctx.arc(lightX, y - 3, 2.2, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.fillStyle = "rgba(255,210,88,0.85)";
  ctx.font = "700 10px Consolas, monospace";
  ctx.textAlign = "center";
  ctx.fillText("TRANQUILITY BASE", (left + right) / 2, y + 18);
}

function drawTrail(camera) {
  if (trail.length < 2) {
    return;
  }

  ctx.lineWidth = 1.6;
  for (let index = 1; index < trail.length; index += 1) {
    const alpha = index / trail.length * 0.5;
    ctx.strokeStyle = `rgba(91,175,255,${alpha.toFixed(3)})`;
    ctx.beginPath();
    ctx.moveTo(camera.x(trail[index - 1].x), camera.y(trail[index - 1].y));
    ctx.lineTo(camera.x(trail[index].x), camera.y(trail[index].y));
    ctx.stroke();
  }
}

function drawDust(camera) {
  if (!controls.thrust || altitudeAboveGround() > 85 || gameState !== "flying") {
    return;
  }

  const groundY = camera.y(localTerrainHeight(lander.x));
  const landerX = camera.x(lander.x);
  const strength = 1 - altitudeAboveGround() / 85;
  ctx.strokeStyle = `rgba(194,190,177,${(0.18 + strength * 0.5).toFixed(2)})`;
  ctx.lineWidth = 1.2;
  for (let index = 0; index < 14; index += 1) {
    const direction = index % 2 === 0 ? -1 : 1;
    const spread = 14 + index * 3.5 * strength;
    ctx.beginPath();
    ctx.moveTo(landerX + direction * 4, groundY - 1 - Math.random() * 3);
    ctx.lineTo(landerX + direction * spread, groundY - Math.random() * 6);
    ctx.stroke();
  }
}

function drawLander(camera) {
  const x = camera.x(lander.x);
  const y = camera.y(lander.y);
  const size = clamp(35 + (1 - clamp(altitudeAboveGround() / 500, 0, 1)) * 22, 35, 57);
  const scale = size / 48;
  const engineActive = controls.thrust && lander.fuel > 0 && gameState === "flying";
  const crashed = gameState === "crashed";

  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(lander.angle);
  ctx.scale(scale, scale);
  ctx.globalAlpha = crashed ? 0.82 : 1;

  if (engineActive) {
    const flicker = 36 + Math.random() * 13;
    const flame = ctx.createLinearGradient(0, 18, 0, flicker);
    flame.addColorStop(0, "#fff8c9");
    flame.addColorStop(0.24, "#ffd04d");
    flame.addColorStop(0.72, "#ff7b1d");
    flame.addColorStop(1, "rgba(255,70,10,0)");
    ctx.fillStyle = flame;
    ctx.beginPath();
    ctx.moveTo(-6, 15);
    ctx.quadraticCurveTo(-3, 29, 0, flicker);
    ctx.quadraticCurveTo(3, 29, 6, 15);
    ctx.closePath();
    ctx.fill();
  }

  ctx.strokeStyle = "#bfc8d3";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(-15, 11);
  ctx.lineTo(-29, 25);
  ctx.lineTo(-35, 25);
  ctx.moveTo(15, 11);
  ctx.lineTo(29, 25);
  ctx.lineTo(35, 25);
  ctx.moveTo(-12, 9);
  ctx.lineTo(-22, 21);
  ctx.moveTo(12, 9);
  ctx.lineTo(22, 21);
  ctx.stroke();

  const foil = ctx.createLinearGradient(-18, 0, 18, 16);
  foil.addColorStop(0, "#826015");
  foil.addColorStop(0.32, "#f1c84f");
  foil.addColorStop(0.62, "#8e6714");
  foil.addColorStop(1, "#e6bd3b");
  ctx.fillStyle = foil;
  ctx.strokeStyle = "#4d3b12";
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(-17, 4);
  ctx.lineTo(-20, 15);
  ctx.lineTo(-10, 20);
  ctx.lineTo(10, 20);
  ctx.lineTo(20, 15);
  ctx.lineTo(17, 4);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = "#d6dbe2";
  ctx.strokeStyle = "#687586";
  ctx.beginPath();
  ctx.moveTo(-11, 4);
  ctx.lineTo(-9, -12);
  ctx.lineTo(-3, -20);
  ctx.lineTo(10, -16);
  ctx.lineTo(16, -4);
  ctx.lineTo(13, 5);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = "#152337";
  ctx.beginPath();
  ctx.moveTo(-6, -11);
  ctx.lineTo(-2, -17);
  ctx.lineTo(3, -16);
  ctx.lineTo(2, -9);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(6, -13);
  ctx.lineTo(11, -10);
  ctx.lineTo(12, -5);
  ctx.lineTo(6, -6);
  ctx.closePath();
  ctx.fill();

  ctx.strokeStyle = "#c9d3df";
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(2, -18);
  ctx.lineTo(4, -28);
  ctx.lineTo(8, -31);
  ctx.stroke();

  if (activeAlarmCode) {
    ctx.strokeStyle = "#ff6266";
    ctx.lineWidth = 2.5;
    ctx.strokeRect(-22, -23, 44, 46);
  }

  if (crashed) {
    ctx.strokeStyle = "#ff665f";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(-12, -4);
    ctx.lineTo(-3, 3);
    ctx.lineTo(-9, 12);
    ctx.moveTo(13, -2);
    ctx.lineTo(4, 6);
    ctx.lineTo(11, 14);
    ctx.stroke();
  }

  ctx.restore();

  if (gameState === "flying") {
    const vectorScale = clamp(1.1 - altitudeAboveGround() / 800, 0.3, 1.1);
    drawArrow(
      x,
      y,
      x + lander.vx * 2.2 * vectorScale,
      y + (-lander.vy) * 2.2 * vectorScale,
      "rgba(117,200,255,0.82)",
    );
  }
}

function drawParticles(camera) {
  for (const particle of particles) {
    const alpha = clamp(particle.life / 2, 0, 1);
    ctx.globalAlpha = alpha;
    ctx.fillStyle = particle.color;
    ctx.beginPath();
    ctx.arc(
      camera.x(particle.x),
      camera.y(particle.y),
      particle.size,
      0,
      Math.PI * 2,
    );
    ctx.fill();
    ctx.globalAlpha = 1;
  }
}

function drawScene(now) {
  ctx.setTransform(viewport.dpr, 0, 0, viewport.dpr, 0, 0);
  ctx.clearRect(0, 0, viewport.width, viewport.height);
  const camera = createCamera();
  drawBackground(camera, now);
  drawTerrain(camera);
  drawTrail(camera);
  drawDust(camera);
  drawLander(camera);
  drawParticles(camera);
}

function frame(now) {
  const deltaSeconds = clamp((now - lastFrameTime) / 1000, 0, 0.05);
  lastFrameTime = now;

  if (gameState === "flying") {
    updatePhysics(deltaSeconds);
    if (gameState === "flying") {
      updateMission(deltaSeconds);
      telemetryAccumulator += deltaSeconds;
      if (telemetryAccumulator >= TELEMETRY_INTERVAL_SECONDS) {
        telemetryAccumulator %= TELEMETRY_INTERVAL_SECONDS;
        sendGameTelemetry();
      }
    }
  }

  updateParticles(deltaSeconds);
  updateTimeline();
  renderHud();
  drawScene(now);
  requestAnimationFrame(frame);
}

function bindControls() {
  const keyboardBindings = {
    KeyA: "left",
    ArrowLeft: "left",
    KeyD: "right",
    ArrowRight: "right",
    KeyW: "thrust",
    ArrowUp: "thrust",
    Space: "thrust",
  };

  window.addEventListener("keydown", (event) => {
    if (event.code === "KeyM") {
      event.preventDefault();
      toggleBackgroundAudio();
      return;
    }
    if (event.code === "KeyR") {
      event.preventDefault();
      restartGame();
      return;
    }
    if (event.code === "Enter" && gameState !== "flying") {
      event.preventDefault();
      startGame();
      return;
    }
    const control = keyboardBindings[event.code];
    if (control) {
      event.preventDefault();
      setControl(control, true);
    }
  });

  window.addEventListener("keyup", (event) => {
    const control = keyboardBindings[event.code];
    if (control) {
      event.preventDefault();
      setControl(control, false);
    }
  });

  for (const [control, button] of controlButtons) {
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      button.setPointerCapture(event.pointerId);
      setControl(control, true);
    });
    button.addEventListener("pointerup", (event) => {
      event.preventDefault();
      setControl(control, false);
    });
    button.addEventListener("pointercancel", () => setControl(control, false));
    button.addEventListener("contextmenu", (event) => event.preventDefault());
  }

  window.addEventListener("blur", clearControls);
  dom.startButton.addEventListener("click", startGame);
  dom.restartButton.addEventListener("click", restartGame);
  dom.soundButton.addEventListener("click", toggleBackgroundAudio);
  dom.playerIdButton.addEventListener("click", () => {
    copyIdentifier(playerId, "PLAYER ID");
  });
  dom.gameIdButton.addEventListener("click", () => {
    copyIdentifier(attemptId, "GAME ID");
  });
}

async function initialize() {
  dom.startButton.disabled = true;
  resizeCanvas();
  window.addEventListener("resize", resizeCanvas);
  await loadMissionTimeline();
  resetMissionState();
  dom.backgroundAudio.volume = 0.42;
  updateSoundButton();
  bindControls();
  dom.startButton.disabled = false;
  connectGameSocket().catch(scheduleSocketReconnect);
  addLog("system", MISSION_START_GET, "SIMULATOR READY - physics and telemetry online");
  await pollHealth();
  setInterval(pollHealth, 3000);
  requestAnimationFrame(frame);
}

initialize().catch((error) => {
  dom.overlayTitle.textContent = "SIMULATOR FAILED TO START";
  dom.overlayCopy.textContent = error.message;
  dom.startButton.disabled = true;
  dom.connectionStatus.textContent = "ERROR";
  console.error(error);
});
