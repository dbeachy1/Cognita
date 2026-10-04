# Reaching Cognita from claude.ai, ChatGPT and other web clients

Cognita runs on your own machine and listens only on `localhost`. AI clients that run
in the cloud, such as claude.ai and ChatGPT, cannot reach `localhost`. They need a
public **HTTPS** address that forwards to Cognita's MCP port (8675 by default). A
*tunnel* gives you that address without opening a port on your router.

Whatever you use, it must give you an address that **never changes**. Your connectors
and their OAuth sign-in are tied to that address. If it changes, every connector has to
be created again.

Only the MCP port goes through the tunnel. **Never** put the Admin port (8676 by
default) on the internet.

## Choosing

| Option | Cost | Needs your own domain | Address | Good for |
|---|---|---|---|---|
| **Tailscale Funnel** (recommended) | Free | No | `https://<machine>.<tailnet>.ts.net` | Most people. The installer can set it up. |
| **Cloudflare Tunnel** | Free | Yes, with its DNS on Cloudflare | `https://cognita.your-domain.com` | People who already run a domain on Cloudflare. |
| **Your own reverse proxy** | Your server or router | Yes | Your own | People who already run Caddy, nginx or Traefik with a public IP. |
| ngrok (free plan) | Free, with low caps | No | `https://<name>.ngrok-free.app` | Short tests only. See below. |

Do **not** use Cloudflare's no-account "quick tunnel" (`cloudflared tunnel --url ...`).
It gets a new random address every time it starts, which breaks your connectors.

## Tailscale Funnel (recommended)

Funnel publishes one local port at a fixed HTTPS address under your Tailscale account.
It is free on the Personal plan, gives you a real certificate, and needs no domain.

1. Create a free account at [tailscale.com](https://tailscale.com) and install Tailscale
   on the machine that runs Cognita. On Windows, install the Windows app, not a copy
   inside WSL: Cognita's port is already reachable at `localhost` from Windows.
2. Sign in: `tailscale up` (Linux) or the tray app (Windows).
3. Publish the MCP port:

   ```bash
   tailscale funnel --bg 8675
   ```

   The first time, Tailscale prints a link to turn on HTTPS and Funnel for your
   account. Open it, approve, and run the command again. It prints your public
   address, for example `https://mybox.tail1234.ts.net`.
4. Check it: `tailscale funnel status`.

`--bg` keeps the Funnel running after you close the terminal and across reboots. To
stop publishing: `tailscale funnel --https=443 off`.

Your address includes the machine's Tailscale name. Renaming the machine in Tailscale
changes the address, so pick the name before you create connectors.

Funnel traffic has a bandwidth cap that Tailscale does not publish. MCP traffic is
small; normal use does not come near it.

## Cloudflare Tunnel

Use this if you already have a domain whose DNS is managed by Cloudflare. The tunnel
itself is free.

1. In the Cloudflare dashboard, open **Zero Trust → Networks → Tunnels** and create a
   tunnel (type *Cloudflared*). Give it a name, for example `cognita`.
2. Cloudflare shows an install command for your OS with a token in it. Run it on the
   machine that runs Cognita. It installs `cloudflared` as a system service, so the
   tunnel comes back after a reboot. (On Windows, run the Windows command in an
   elevated PowerShell.)
3. Add a **public hostname**: for example `cognita` on `your-domain.com`, service type
   **HTTP**, URL `localhost:8675`.
4. Your public address is `https://cognita.your-domain.com`. Cloudflare provides the
   certificate; Cognita itself stays plain HTTP on `localhost`.

The token in the install command is a secret. Anyone with it can run your tunnel.
Don't paste it into chats, issues or screenshots.

## Your own reverse proxy

If you already have a server with a public IP and a domain, point a hostname at it and
proxy HTTPS to Cognita's MCP port. Two rules:

- Proxy to the MCP port only, over a private link (VPN, SSH tunnel, or the same machine).
- Don't buffer responses, and allow long request times. Some tool calls take a while.

## ngrok

The free plan gives you one fixed address, but it allows only 1 GB and 20,000 requests a
month. It also shows a warning page in front of anything opened in a browser, which gets
in the way of the connector's sign-in. Fine for a quick test; not for daily use.

## After the tunnel is up

1. **Tell Cognita its public address.** In Admin, open **Connectors** and find
   **Canonical public URL**. Enter the address with no path (for example `https://mybox.tail1234.ts.net`),
   and save. Cognita uses it for the OAuth sign-in your connectors perform.
2. **Check it from outside:**

   ```bash
   curl -s https://<your-address>/healthz
   ```

   You should see `{"status":"ok","service":"cognita","version":"..."}`.
3. **Create the connector.** In Admin, open **Connectors**, copy the connector's
   **Stable MCP URL**, and add it as a custom connector in claude.ai or ChatGPT. The
   client sends you to Cognita's sign-in page once.

### What the errors mean

| You see | Meaning | Fix |
|---|---|---|
| `/healthz` returns the JSON above | The tunnel and Cognita both work. | Nothing. |
| **401** when you open the MCP URL directly | Normal. The MCP URL requires sign-in; the tunnel works. | Nothing; use it from the connector. |
| **502**, **503** or **530** | The tunnel is up, but it can't reach Cognita. | Check Cognita is running (`status` command) and the tunnel points at `localhost:8675`. |
| **404** | The address works but the path is wrong. | Copy the Stable MCP URL from Admin again. |
| Timeout or DNS error | The tunnel isn't running, or the address is wrong. | Check `tailscale funnel status` or the Cloudflare tunnel's status page. |
| The connector's sign-in fails after it redirects | Cognita's public URL doesn't match the tunnel address. | Fix **Canonical public URL** on Admin's Connectors tab. |

## Security

- The tunnel exposes Cognita's MCP endpoint to the internet. Every call still needs the
  connector's OAuth sign-in or its private key; nothing is readable without one.
- A connector with Read/write access can change and delete documents in those
  projects. In Admin, give each connector only the projects it needs, and choose
  **Read-only** for any project the AI should never change.
- Keep Admin on `localhost` or your LAN. It is never meant to go through the tunnel.
