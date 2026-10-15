Version 16.1.6 carries this change; 16.1.5 was withdrawn because its sign-in pages failed Cognita's cross-site request check in Chrome.

Cognita now accepts OAuth sign-ins from any MCP client. Earlier releases accepted only Claude, ChatGPT, OpenAI, and localhost. Other clients, such as TypingMind, were refused when they tried to register (`redirect_uris contains an unsupported URI`).

**Latest verified platform installers (16.1.0; these do not include this patch):**
[Windows installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/Cognita-Setup-16.1.0-r1.exe) |
[Linux source installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/cognita-src-16.1.0.tar.gz).

This is a source patch for existing installations. No new Windows executable or installer is supplied by this source patch.

**What controls access now.** Any client can register, but nothing is granted until you sign in on Cognita's approval page with your Cognita password and click Authorize. A client chooses its own name, so the approval page now also says which site the access is sent to. Approve only if that is the client you are connecting.

**What is still refused.** A callback address must be HTTPS, or plain HTTP to this machine (`localhost`, `127.0.0.1`, `::1`), with no user name, password, or fragment in it. This is checked when a client registers and again on every authorization, so a client that registered through a metadata document is held to the same rule. A callback host written with non-ASCII lookalike characters (for example a Cyrillic letter that looks like a Latin one) is refused, so the approval page cannot be made to show a familiar name for a different site; an internationalized domain still works in its `xn--` form. Client metadata and signing-key fetches still refuse private and internal addresses, redirects, and oversized or slow responses.

**Limits on metadata-URL clients.** Because any site can now present a metadata URL, a URL seen for the first time is accepted only while there is room under the same 100-client cap that dynamic registration uses, and only 10 new URLs are accepted per hour. Clients already registered always refresh. A failed metadata fetch backs off rather than being retried on every request.

**Security headers fixed.** The approval and sign-in pages set a content security policy, frame blocking, a referrer policy and nosniff, but the OAuth proxy dropped those headers before they reached the browser (since 7.0). They are now forwarded. The approval page's policy allows its form to continue only to the callback site validated for that request.

**Configuration.** `oauth_allowed_client_hosts` and `oauth_cimd_allowed_hosts` are removed. If your `cognita.yaml` still lists them, it loads unchanged and the keys are ignored. You can delete them at your convenience.

Existing connectors and their grants keep working. MCP arguments, results, and connector generations are unchanged (combined v6, Workspace v3).
