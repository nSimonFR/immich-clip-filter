// immich-clip-filter — a CLIP content filter step for Immich Workflows.
//
// Immich 3.1's core plugin can filter on filename, EXIF, type, date and
// location, but not on what is actually IN the picture. This adds that step, so
// a workflow is just:
//
//   AssetMetadataExtraction -> clip-filter#clipFilter(seedAlbum="Food seeds", albumIds=[…])
//
// A rule can name a seed ALBUM, whose members the sidecar reads live, so a whole
// rule is definable in the Immich UI with no shell involved.
//
// It does NOT do the inference itself, and cannot: the `httpRequest` host
// function returns `body: await res.text()` (see
// services/workflow-execution.service.js), so no image bytes can come in and no
// multipart can go out. All this does is hand the asset id to the sidecar
// (the immich-clip-filter sidecar) and act on its verdict.
//
// ⚠️ WHY THIS IS ONE STEP that carries albumIds, instead of a pure filter with
// `immich-plugin-core#assetAddToAlbums` chained after it:
//
//   Immich 3.1's `WorkflowRepository.getForWorkflowRun` selects workflow_step
//   WITHOUT `ORDER BY "order"`. Postgres is then free to return the steps in any
//   order, and it does — the same query returned
//   [typeFilter, addToAlbums, clipFilter] on one call and the declared order on
//   the next. When the add lands first, EVERY asset is filed regardless of the
//   verdict; observed, not theorised. So a filter step cannot reliably gate a
//   later action step on this version, and the only safe workflow is one step.
//
//   Hence: this step checks the asset type itself and hands albumIds to the
//   sidecar, which does the filing. It still returns workflow.continue, so
//   chaining works again once upstream orders the query.
//
// Fail-closed on purpose: sidecar down, bad JSON, unknown profile — each means
// "do not file", because a false positive files the whole camera roll while a
// false negative loses one photo. The one case that is NOT a no is an asset with
// no embedding yet (beast, the ML host, is usually off): the sidecar queues
// those and immich-clip-drain files them later, so nothing is silently lost.
//
// Everything gets logged; extism wires console.log to the Immich logger, so it
// surfaces as `Plugin:clip-filter@<version>`.

const { httpRequest } = Host.getFunctions();

const DEFAULTS = {
  // No default rule: a step names either a seedAlbum or a profile, and guessing
  // "food" here would silently apply the wrong rule to a half-filled config.
  profile: "",
  threshold: 0.28,
  waitSec: 60,
  sidecar: "http://127.0.0.1:8351/classify",
  assetTypes: ["IMAGE"],
};

function halt(reason) {
  console.log("no match: " + reason);
  Host.outputString(JSON.stringify({ workflow: { continue: false } }));
}

function clipFilter() {
  let payload;
  try {
    payload = JSON.parse(Host.inputString());
  } catch (e) {
    return halt("unparseable step payload: " + e);
  }

  const config = payload.config || {};
  const asset = (payload.data || {}).asset || {};
  if (!asset.id) {
    return halt("step payload carried no asset id");
  }

  // Done here rather than by chaining assetTypeFilter, for the ordering reason
  // above — and because a video has no CLIP embedding, so letting one through
  // would burn the full waitSec before concluding nothing.
  const types = config.assetTypes || DEFAULTS.assetTypes;
  if (asset.type && types.indexOf(asset.type) === -1) {
    return halt("asset type " + asset.type + " is not in " + JSON.stringify(types));
  }

  // albumIds goes TO the sidecar rather than being acted on here: the sidecar
  // owns filing for both the immediate path and the deferred one (an asset with
  // no embedding yet is queued and filed later by immich-clip-drain), so there
  // is one implementation instead of two, and it is testable in Python.
  const body = JSON.stringify({
    assetId: asset.id,
    profile: config.profile || DEFAULTS.profile,
    threshold: config.threshold != null ? config.threshold : DEFAULTS.threshold,
    waitSec: config.waitSec != null ? config.waitSec : DEFAULTS.waitSec,
    albumIds: config.albumIds || [],
    // Both optional; the sidecar prefers seedAlbum when set, which is what lets
    // a whole rule be defined in the Immich UI with no shell involved.
    seedAlbum: config.seedAlbum || "",
    scoring: config.scoring || "",
  });

  // The host wrapper expects {authToken, args} and applies `fetch(...args)`,
  // checking args[0]'s hostname against the manifest's allowedHosts first.
  const request = JSON.stringify({
    authToken: (payload.workflow || {}).authToken,
    args: [
      config.sidecar || DEFAULTS.sidecar,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
      },
    ],
  });

  let result;
  try {
    const offset = httpRequest(Memory.fromString(request).offset);
    result = JSON.parse(Memory.find(offset).readString());
  } catch (e) {
    return halt("sidecar call threw: " + e);
  }

  if (!result.success) {
    return halt("host refused the call: " + (result.message || "unknown"));
  }
  const response = result.response || {};
  if (!response.ok) {
    return halt("sidecar returned HTTP " + response.status);
  }

  let verdict;
  try {
    verdict = JSON.parse(response.body);
  } catch (e) {
    return halt("sidecar body was not JSON: " + e);
  }

  if (verdict.match !== true) {
    // `undecided` is not a no: the asset simply has no embedding yet, because
    // CLIP runs on beast and beast is usually off. The sidecar has queued it and
    // immich-clip-drain will file it once Immich catches up. Halting here is
    // still right — there is nothing more this run can do.
    return halt(verdict.queued
      ? "queued until the ML server catches up"
      : verdict.reason || "distance " + verdict.distance + " over threshold");
  }

  console.log("match: " + asset.id + " at distance " + verdict.distance +
              " (filed into " + (verdict.filed || 0) + " album(s))");
  Host.outputString(JSON.stringify({ workflow: { continue: true } }));
}

module.exports = { clipFilter };
