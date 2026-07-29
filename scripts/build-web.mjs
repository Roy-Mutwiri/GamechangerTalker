// Build the hosted copy of the browser UI.
//
// The page has exactly one source of truth -- narrator/ui/web/index.html, the
// file the narrator itself serves on localhost. This copies it into public/
// and bakes in the relay URL, so the hosted copy and the local one can never
// drift into being two different pages that behave differently.
//
// NARRATOR_RELAY is the wss:// address of the machine actually running the
// narrator (a Cloudflare tunnel to its websocket port). Without it the page
// still builds and still works locally; hosted, it will sit at "connecting"
// until someone passes ?relay= by hand, which is the honest failure -- better
// than a page that looks fine and silently shows nothing.

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(root, "narrator", "ui", "web", "index.html");
const outDir = join(root, "public");

const relay = (process.env.NARRATOR_RELAY || "").trim();
let html = await readFile(source, "utf8");

if (relay) {
  if (!relay.startsWith("wss://")) {
    // A https page cannot open a ws:// socket; browsers block it as mixed
    // content. Failing the build is kinder than shipping a page that never
    // connects and never says why.
    throw new Error(`NARRATOR_RELAY must start with wss:// (got ${relay})`);
  }
  html = html.replace(
    "<head>",
    `<head>\n<script>window.NARRATOR_RELAY = ${JSON.stringify(relay)};</script>`,
  );
  console.log(`relay baked in: ${relay}`);
} else {
  console.warn("NARRATOR_RELAY is not set; the hosted page will need ?relay=");
}

await mkdir(outDir, { recursive: true });
await writeFile(join(outDir, "index.html"), html, "utf8");
console.log(`wrote ${join(outDir, "index.html")} (${html.length} bytes)`);
