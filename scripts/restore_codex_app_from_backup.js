const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const appPath = process.argv[2] || "/Applications/Codex.app";
const backupDir = path.join(__dirname, "..", "app-backups");
const resourcesPath = path.join(appPath, "Contents", "Resources");
const asarPath = path.join(resourcesPath, "app.asar");
const plistPath = path.join(appPath, "Contents", "Info.plist");

function latestPair() {
  const files = fs.readdirSync(backupDir);
  const stamps = files
    .map((file) => file.match(/^(app\.asar|Info\.plist)\.history-repair-backup-(\d{14})$/))
    .filter(Boolean)
    .map((match) => match[2])
    .sort()
    .reverse();

  for (const stamp of stamps) {
    const asarBackup = path.join(backupDir, `app.asar.history-repair-backup-${stamp}`);
    const plistBackup = path.join(backupDir, `Info.plist.history-repair-backup-${stamp}`);
    if (fs.existsSync(asarBackup) && fs.existsSync(plistBackup)) {
      return { stamp, asarBackup, plistBackup };
    }
  }
  throw new Error(`No complete backup pair found in ${backupDir}`);
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
  const backup = latestPair();
  fs.copyFileSync(backup.asarBackup, asarPath);
  fs.copyFileSync(backup.plistBackup, plistPath);
  resignApp();
  console.log(
    JSON.stringify(
      {
        status: "ok",
        appPath,
        restored: backup,
        resigned: true,
      },
      null,
      2,
    ),
  );
}

main();
