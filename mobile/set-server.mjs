// Point the app at a Shelfie server, e.g.
//   node set-server.mjs https://shelfie-yourname.fly.dev
//   node set-server.mjs http://192.168.1.20:8000
// then rebuild the APK (npm run apk).
import { readFileSync, writeFileSync } from "node:fs";

const url = (process.argv[2] || "").replace(/\/+$/, "");
if (!/^https?:\/\/[^/]+$/.test(url)) {
  console.error("Usage: node set-server.mjs <http(s)://host[:port]>");
  process.exit(1);
}
const cfgPath = new URL("./capacitor.config.json", import.meta.url);
const cfg = JSON.parse(readFileSync(cfgPath, "utf8"));
cfg.server.url = url;
cfg.server.cleartext = url.startsWith("http://"); // plain http only for a server on your own network
writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n");
writeFileSync(new URL("./www/config.js", import.meta.url),
  `// written by set-server.mjs; the address the app loads Shelfie from\nwindow.SHELFIE_URL = ${JSON.stringify(url)};\n`);
console.log(`Shelfie app now points at ${url}. Rebuild with: npm run apk`);
