# Sharing the stream

Two links, both served by Vercel:

| URL | What it is |
|---|---|
| `https://<project>.vercel.app/` | the operator UI — transcript, facts, avatar picker, camera, podcast toggle |
| `https://<project>.vercel.app/stage` | the characters only, full bleed. The one to put in an OBS browser source or hand to someone who wants to watch rather than drive |

## What Vercel is actually hosting

The page, and nothing else.

This project is a Windows desktop pipeline: MetaTrader 5 for prices, Kokoro on
a local GPU for speech, Ollama for the hosts, and Warudo rendering the
characters. None of that can run on Vercel, which serves static files and
short-lived serverless functions and has no GPU, no persistent process and no
way to talk to a terminal on your desk.

So the split is:

    Vercel  ->  the page             (public, always up)
    your PC ->  the prices, the voices, the characters, the websocket

The page connects back to your machine over a Cloudflare tunnel. **When your PC
is not running the narrator, the links load and sit at "connecting".** That is
the honest behaviour and not a fault to debug.

## The tunnel

Cloudflare Tunnel dials *out* from your machine, so there is no port
forwarding, no firewall hole, and your home IP is never published.

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel login                       # opens a browser, one time
cloudflared tunnel create narrator
```

Route the tunnel at the narrator's **websocket** port — 8771, one above the
page's 8770. That port is the whole feed: state, transcript, viseme tracks and
the Warudo frames all arrive on it.

```yaml
# %USERPROFILE%\.cloudflared\config.yml
tunnel: narrator
credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json

ingress:
  - hostname: narrator.<your-domain>
    service: ws://127.0.0.1:8771
  - service: http_status:404
```

```powershell
cloudflared tunnel route dns narrator narrator.<your-domain>
cloudflared tunnel run narrator
```

Leave that running alongside the narrator.

## Wiring the page to the tunnel

Set `NARRATOR_RELAY` in the Vercel project (Settings -> Environment Variables)
to the tunnel's websocket address, then redeploy:

    NARRATOR_RELAY = wss://narrator.<your-domain>

`scripts/build-web.mjs` bakes it into the page at build time. It must be
`wss://`, not `ws://` — a page served over https is not allowed to open a
plaintext socket, and browsers block it silently. The build fails loudly rather
than shipping a page that never connects.

Without the variable the page still builds; you can then point it anywhere by
hand with `?relay=wss://...`, which is the quickest way to test a tunnel before
committing it to the project settings.

## One source of truth

`narrator/ui/web/index.html` is the page. The narrator serves it on localhost
and the build copies that same file into `public/` for Vercel. There is no
second copy to keep in sync — `public/` is generated and gitignored.

## Open access

The websocket takes operator commands: text the hosts will read aloud, avatar
switches, camera moves. There is **no authentication on it**, by decision.
Anyone with the tunnel URL can drive the stream, including making the
characters say things you did not write.

If that changes, the fix is a token checked in `WebUI._client` before the
first message is accepted, and passed by the page as `?key=` — perhaps twenty
lines. The tunnel URL is the only thing keeping it private today, so treat it
as the password it currently is.
