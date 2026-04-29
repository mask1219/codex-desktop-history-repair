const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  buildCheckStatus,
  findPatchTargets,
  patches,
  readAsarHeader,
} = require("../scripts/patch_codex_app_extended_history.js");

function writeFakeAsar(filePath, files) {
  const header = { files: {} };
  const dataBuffers = [];
  let offset = 0;

  for (const [file, text] of Object.entries(files)) {
    const content = Buffer.from(text, "utf8");
    let node = header.files;
    const parts = file.split("/");
    for (const part of parts.slice(0, -1)) {
      node[part] = node[part] || { files: {} };
      node = node[part].files;
    }
    node[parts[parts.length - 1]] = {
      offset,
      size: content.length,
    };
    dataBuffers.push(content);
    offset += content.length;
  }

  const headerBuffer = Buffer.from(JSON.stringify(header), "utf8");
  const prefix = Buffer.alloc(16);
  prefix.writeUInt32LE(headerBuffer.length, 12);
  fs.writeFileSync(filePath, Buffer.concat([prefix, headerBuffer, ...dataBuffers]));
}

function buildPatchedFixtureFiles() {
  return {
    "webview/assets/vscode-api-random.js": [
      "persistExtendedHistory:!0",
    ].join("\n"),
    "webview/assets/app-server-manager-signals-random.js": [
      "persistExtendedHistory:!0",
      "sortKey:this.sortKey,modelProviders:[],useStateDbOnly:!0",
      "let r=[],i=async a=>{let o=await e.sendRequest(`thread/list`,{limit:200,cursor:a,sortKey:e.recentConversationsSortKey,modelProviders:t,useStateDbOnly:!0,archived:n});r.push(...o.data),o.nextCursor&&await i(o.nextCursor)};return await i(),r",
      "C=n??c?.settings.model??null,w=await e.buildNewConversationParams",
      "modelProvider:null,serviceTier",
      "config:null,",
    ].join("\n"),
    ".vite/build/workspace-root-drop-handler-random.js": [
      "sortKey:`updated_at`,modelProviders:[],useStateDbOnly:!0",
    ].join("\n"),
  };
}

test("check status reports stale plist hash even when app.asar is already patched", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "history-repair-patch-check-"));
  const asarPath = path.join(tmpDir, "app.asar");
  const plistPath = path.join(tmpDir, "Info.plist");

  writeFakeAsar(asarPath, buildPatchedFixtureFiles());
  fs.writeFileSync(
    plistPath,
    [
      "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
      "<plist version=\"1.0\">",
      "<dict>",
      "<key>hash</key>",
      "<string>0000000000000000000000000000000000000000000000000000000000000000</string>",
      "</dict>",
      "</plist>",
    ].join("\n"),
    "utf8",
  );

  const result = buildCheckStatus({
    targetAsarPath: asarPath,
    targetPlistPath: plistPath,
  });

  assert.equal(result.patchResult.changed, false);
  assert.equal(result.hashResult.changed, true);
  assert.equal(result.needsPatch, true);
});

test("content fallback finds multi-pattern patches after hashed filenames change", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "history-repair-patch-fallback-"));
  const asarPath = path.join(tmpDir, "app.asar");
  writeFakeAsar(asarPath, {
    "random/renamed-bundle.js": "sortKey:this.sortKey,modelProviders:null,archived:!1,sourceKinds:w",
  });

  const buffer = fs.readFileSync(asarPath);
  const { header, dataOffset } = readAsarHeader(buffer);
  const matches = findPatchTargets(buffer, header, dataOffset, patches[2]);

  assert.equal(matches.length, 1);
  assert.equal(matches[0].file, "random/renamed-bundle.js");
});

test("content fallback finds current app-server sourceKinds symbol", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "history-repair-patch-source-kinds-"));
  const asarPath = path.join(tmpDir, "app.asar");
  writeFakeAsar(asarPath, {
    "random/renamed-bundle.js": "sortKey:this.sortKey,modelProviders:null,archived:!1,sourceKinds:T",
  });

  const buffer = fs.readFileSync(asarPath);
  const { header, dataOffset } = readAsarHeader(buffer);
  const matches = findPatchTargets(buffer, header, dataOffset, patches[2]);

  assert.equal(matches.length, 1);
  assert.equal(matches[0].file, "random/renamed-bundle.js");
});

test("content fallback finds resume model preference patch", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "history-repair-patch-resume-model-"));
  const asarPath = path.join(tmpDir, "app.asar");
  writeFakeAsar(asarPath, {
    "random/renamed-bundle.js":
      "C=n??l?.latestCollaborationMode.settings.model??c?.settings.model??null,w=await e.buildNewConversationParams",
  });

  const buffer = fs.readFileSync(asarPath);
  const { header, dataOffset } = readAsarHeader(buffer);
  const matches = findPatchTargets(buffer, header, dataOffset, patches[5]);

  assert.equal(matches.length, 1);
  assert.equal(matches[0].file, "random/renamed-bundle.js");
});

test("content fallback finds resume provider patch", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "history-repair-patch-resume-provider-"));
  const asarPath = path.join(tmpDir, "app.asar");
  writeFakeAsar(asarPath, {
    "random/renamed-bundle.js": "modelProvider:w.modelProvider,serviceTier",
  });

  const buffer = fs.readFileSync(asarPath);
  const { header, dataOffset } = readAsarHeader(buffer);
  const matches = findPatchTargets(buffer, header, dataOffset, patches[6]);

  assert.equal(matches.length, 1);
  assert.equal(matches[0].file, "random/renamed-bundle.js");
});

test("content fallback finds resume config patch", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "history-repair-patch-resume-config-"));
  const asarPath = path.join(tmpDir, "app.asar");
  writeFakeAsar(asarPath, {
    "random/renamed-bundle.js": "config:w.config,",
  });

  const buffer = fs.readFileSync(asarPath);
  const { header, dataOffset } = readAsarHeader(buffer);
  const matches = findPatchTargets(buffer, header, dataOffset, patches[7]);

  assert.equal(matches.length, 1);
  assert.equal(matches[0].file, "random/renamed-bundle.js");
});
