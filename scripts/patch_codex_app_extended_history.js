const crypto = require("node:crypto");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const args = process.argv.slice(2);
let appPath = "/Applications/Codex.app";
let selectedPatchName = "all";
let checkOnly = false;
for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--patch") {
    selectedPatchName = args[i + 1];
    i += 1;
  } else if (args[i] === "--check") {
    checkOnly = true;
  } else {
    appPath = args[i];
  }
}
const backupDir = path.join(__dirname, "..", "app-backups");
const resourcesPath = path.join(appPath, "Contents", "Resources");
const asarPath = path.join(resourcesPath, "app.asar");
const plistPath = path.join(appPath, "Contents", "Info.plist");
const backupStamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);

const patches = [
  {
    name: "vscode-api",
    file: "webview/assets/vscode-api-DUrFyyxA.js",
    filePattern: /^webview\/assets\/vscode-api-.*\.js$/,
    from: "persistExtendedHistory:c?.persistExtendedHistory===!0",
    to: "persistExtendedHistory:!0",
  },
  {
    name: "app-server",
    file: "webview/assets/app-server-manager-signals-BxR9wXcg.js",
    filePattern: /^webview\/assets\/app-server-manager-signals-.*\.js$/,
    from: "persistExtendedHistory:w.persistExtendedHistory",
    to: "persistExtendedHistory:!0",
  },
  {
    name: "thread-list-state-db",
    file: "webview/assets/app-server-manager-signals-BxR9wXcg.js",
    filePattern: /^webview\/assets\/app-server-manager-signals-.*\.js$/,
    from: [
      "sortKey:this.sortKey,modelProviders:null,useStateDbOnly:!0",
      "sortKey:this.sortKey,modelProviders:null,archived:!1,sourceKinds:w",
      "sortKey:this.sortKey,modelProviders:null,archived:!1,sourceKinds:T",
      "sortKey:this.sortKey,useStateDbOnly:!0  ,archived:!1,sourceKinds:w",
      "sortKey:this.sortKey,useStateDbOnly:!0  ,archived:!1,sourceKinds:T",
    ],
    to: "sortKey:this.sortKey,modelProviders:[],useStateDbOnly:!0",
  },
  {
    name: "workspace-root-drop-state-db",
    file: ".vite/build/workspace-root-drop-handler-B7KjFQ8M.js",
    filePattern: /^\.vite\/build\/workspace-root-drop-handler-.*\.js$/,
    from: [
      "sortKey:`updated_at`,modelProviders:null,useStateDbOnly:!0",
      "sortKey:`updated_at`,modelProviders:null,sourceKinds:e.d,archived:!1",
      "sortKey:`updated_at`,useStateDbOnly:!0  ,sourceKinds:e.d,archived:!1",
    ],
    to: "sortKey:`updated_at`,modelProviders:[],useStateDbOnly:!0",
  },
  {
    name: "thread-list-all-state-db",
    file: "webview/assets/app-server-manager-signals-BxR9wXcg.js",
    filePattern: /^webview\/assets\/app-server-manager-signals-.*\.js$/,
    from: [
      "let r=[],i=async a=>{let o=await e.sendRequest(`thread/list`,{limit:200,cursor:a,sortKey:e.recentConversationsSortKey,modelProviders:t,sourceKinds:w,archived:n});r.push(...o.data),o.nextCursor&&await i(o.nextCursor)};return await i(null),r",
      "let r=[],i=async a=>{let o=await e.sendRequest(`thread/list`,{limit:200,cursor:a,sortKey:e.recentConversationsSortKey,modelProviders:t,sourceKinds:T,archived:n});r.push(...o.data),o.nextCursor&&await i(o.nextCursor)};return await i(null),r",
    ],
    to:
      "let r=[],i=async a=>{let o=await e.sendRequest(`thread/list`,{limit:200,cursor:a,sortKey:e.recentConversationsSortKey,modelProviders:t,useStateDbOnly:!0,archived:n});r.push(...o.data),o.nextCursor&&await i(o.nextCursor)};return await i(),r",
  },
  {
    name: "resume-current-model",
    file: "webview/assets/app-server-manager-signals-BxR9wXcg.js",
    filePattern: /^webview\/assets\/app-server-manager-signals-.*\.js$/,
    from:
      "C=n??l?.latestCollaborationMode.settings.model??c?.settings.model??null,w=await e.buildNewConversationParams",
    to:
      "C=n??c?.settings.model??null,w=await e.buildNewConversationParams",
  },
  {
    name: "resume-current-provider",
    file: "webview/assets/app-server-manager-signals-BxR9wXcg.js",
    filePattern: /^webview\/assets\/app-server-manager-signals-.*\.js$/,
    from: "modelProvider:w.modelProvider,serviceTier",
    to: "modelProvider:null,serviceTier",
  },
  {
    name: "resume-load-current-config",
    file: "webview/assets/app-server-manager-signals-BxR9wXcg.js",
    filePattern: /^webview\/assets\/app-server-manager-signals-.*\.js$/,
    from: "config:w.config,",
    to: "config:null,",
  },
];

function patchFromValues(patch) {
  return Array.isArray(patch.from) ? patch.from : [patch.from];
}

function entryTextIncludesPatchSource(text, patch) {
  return patchFromValues(patch).some((value) => text.includes(value));
}

function selectedPatches() {
  if (selectedPatchName === "all") {
    return patches;
  }
  const selected = patches.filter((patch) => patch.name === selectedPatchName);
  if (selected.length === 0) {
    throw new Error(`Unknown patch: ${selectedPatchName}`);
  }
  return selected;
}

function backup(filePath) {
  fs.mkdirSync(backupDir, { recursive: true });
  const dest = path.join(backupDir, `${path.basename(filePath)}.history-repair-backup-${backupStamp}`);
  fs.copyFileSync(filePath, dest);
  return dest;
}

function readAsarHeader(buffer) {
  const headerSize = buffer.readUInt32LE(12);
  const headerStart = 16;
  const header = JSON.parse(buffer.slice(headerStart, headerStart + headerSize).toString("utf8"));
  return { header, dataOffset: headerStart + headerSize };
}

function getEntry(header, file) {
  let node = header;
  for (const part of file.split("/")) {
    node = node.files && node.files[part];
    if (!node) {
      throw new Error(`Missing app.asar entry: ${file}`);
    }
  }
  if (node.files) {
    throw new Error(`Expected file entry, got directory: ${file}`);
  }
  return node;
}

function listEntries(header, prefix = "") {
  const results = [];
  for (const [name, node] of Object.entries(header.files || {})) {
    const file = prefix ? `${prefix}/${name}` : name;
    if (node.files) {
      results.push(...listEntries(node, file));
    } else {
      results.push({ file, entry: node });
    }
  }
  return results;
}

function padReplacement(from, to) {
  if (to.length > from.length) {
    throw new Error(`Replacement is longer than original: ${to}`);
  }
  return `${to}${" ".repeat(from.length - to.length)}`;
}

function readEntryText(buffer, dataOffset, entry) {
  const start = dataOffset + Number(entry.offset || 0);
  const size = Number(entry.size || 0);
  return buffer.slice(start, start + size).toString("utf8");
}

function findPatchTargets(buffer, header, dataOffset, patch) {
  const entries = listEntries(header).filter(({ file }) => file.endsWith(".js"));
  const candidates = entries.filter(({ file }) => file === patch.file || patch.filePattern.test(file));
  const matches = candidates.map(({ file, entry }) => ({
    file,
    entry,
    text: readEntryText(buffer, dataOffset, entry),
  }));
  if (matches.length > 0) {
    return matches;
  }

  // If Codex changes hashed asset names, fall back to content lookup for unpatched targets.
  for (const { file, entry } of entries) {
    const text = readEntryText(buffer, dataOffset, entry);
    if (entryTextIncludesPatchSource(text, patch)) {
      matches.push({ file, entry, text });
    }
  }
  return matches;
}

function hash(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

function updateEntryIntegrity(entry, fileBuffer) {
  if (!entry.integrity) {
    return;
  }
  const blockSize = Number(entry.integrity.blockSize || 4194304);
  const blocks = [];
  for (let i = 0; i < fileBuffer.length; i += blockSize) {
    blocks.push(hash(fileBuffer.slice(i, i + blockSize)));
  }
  entry.integrity.hash = hash(fileBuffer);
  entry.integrity.blocks = blocks;
}

function writeHeader(updated, header, headerSize) {
  const headerBuffer = Buffer.from(JSON.stringify(header), "utf8");
  if (headerBuffer.length !== headerSize) {
    throw new Error("Patch changed app.asar header size");
  }
  headerBuffer.copy(updated, 16);
}

function buildPatchedAsar(targetAsarPath = asarPath) {
  const buffer = fs.readFileSync(targetAsarPath);
  const { header, dataOffset } = readAsarHeader(buffer);
  const headerSize = buffer.readUInt32LE(12);
  const updated = Buffer.from(buffer);
  const results = [];
  let changed = false;

  for (const patch of selectedPatches()) {
    const targets = findPatchTargets(updated, header, dataOffset, patch);
    if (targets.length === 0) {
      throw new Error(`Patch target not found for ${patch.name}`);
    }
    let matched = false;
    for (const target of targets) {
      const entry = target.entry;
      const text = target.text;
      const matchedFrom = patchFromValues(patch).find((value) => text.includes(value));
      if (!matchedFrom) {
        if (text.includes(patch.to)) {
          results.push({ name: patch.name, file: target.file, status: "already-patched" });
          matched = true;
          continue;
        }
        continue;
      }
      matched = true;
      const next = text.replace(matchedFrom, padReplacement(matchedFrom, patch.to));
      if (Buffer.byteLength(next, "utf8") !== Number(entry.size || 0)) {
        throw new Error(`Patch changed file size for ${target.file}`);
      }
      const start = dataOffset + Number(entry.offset || 0);
      const size = Number(entry.size || 0);
      updated.write(next, start, size, "utf8");
      updateEntryIntegrity(entry, updated.slice(start, start + size));
      results.push({ name: patch.name, file: target.file, status: "patched" });
      changed = true;
    }
    if (!matched) {
      throw new Error(`Patch target not found for ${patch.name}`);
    }
  }

  if (changed) {
    writeHeader(updated, header, headerSize);
  }
  return { changed, results, updated };
}

function hashAsarHeaderForPath(targetAsarPath = asarPath) {
  const buffer = fs.readFileSync(targetAsarPath);
  const headerSize = buffer.readUInt32LE(12);
  return hash(buffer.slice(16, 16 + headerSize));
}

function buildUpdatedPlistHash({
  targetAsarPath = asarPath,
  targetPlistPath = plistPath,
} = {}) {
  const asarHeaderHash = hashAsarHeaderForPath(targetAsarPath);
  const plist = fs.readFileSync(targetPlistPath, "utf8");
  const next = plist.replace(
    /(<key>hash<\/key>\s*<string>)[a-f0-9]{64}(<\/string>)/,
    `$1${asarHeaderHash}$2`,
  );
  if (next !== plist) {
    return { changed: true, hash: asarHeaderHash, next };
  }
  if (!plist.includes(asarHeaderHash)) {
    throw new Error("Could not update ElectronAsarIntegrity hash in Info.plist");
  }
  return { changed: false, hash: asarHeaderHash, next: plist };
}

function buildCheckStatus({
  targetAsarPath = asarPath,
  targetPlistPath = plistPath,
} = {}) {
  const patchResult = buildPatchedAsar(targetAsarPath);
  const hashResult = buildUpdatedPlistHash({
    targetAsarPath,
    targetPlistPath,
  });
  return {
    patchResult,
    hashResult,
    needsPatch: patchResult.changed || hashResult.changed,
  };
}

function resignApp() {
  const result = childProcess.spawnSync("codesign", ["--force", "--deep", "--sign", "-", appPath], {
    stdio: "inherit",
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`codesign failed with exit code ${result.status}`);
  }
}

function main() {
  if (!fs.existsSync(asarPath)) {
    throw new Error(`app.asar not found: ${asarPath}`);
  }
  if (checkOnly) {
    const checkResult = buildCheckStatus();
    console.log(
      JSON.stringify(
        {
          status: "ok",
          mode: "check",
          appPath,
          results: checkResult.patchResult.results.map((result) => ({
            ...result,
            status: result.status === "patched" ? "needs-patch" : result.status,
          })),
          needsPatch: checkResult.needsPatch,
          plistHashStatus: checkResult.hashResult.changed ? "needs-patch" : "ok",
          hash: checkResult.hashResult.hash,
        },
        null,
        2,
      ),
    );
    return;
  }
  const patchResult = buildPatchedAsar();
  let asarBackup = null;
  if (patchResult.changed) {
    asarBackup = backup(asarPath);
    fs.writeFileSync(asarPath, patchResult.updated);
  }
  let plistBackup = null;
  if (patchResult.changed) {
    plistBackup = backup(plistPath);
  }
  const hashResult = buildUpdatedPlistHash();
  if (hashResult.changed && plistBackup == null) {
    plistBackup = backup(plistPath);
  }
  if (hashResult.changed) {
    fs.writeFileSync(plistPath, hashResult.next, "utf8");
  }
  const changed = patchResult.changed || hashResult.changed;
  if (changed) {
    resignApp();
  }
  console.log(
    JSON.stringify(
      {
        status: "ok",
        appPath,
        asarBackup,
        plistBackup,
        results: patchResult.results,
        hash: hashResult.hash,
        changed,
        resigned: changed,
      },
      null,
      2,
    ),
  );
}

if (require.main === module) {
  main();
} else {
  module.exports = {
    buildCheckStatus,
    buildUpdatedPlistHash,
    findPatchTargets,
    patchFromValues,
    patches,
    readAsarHeader,
  };
}
