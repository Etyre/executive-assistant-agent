# Atlas apps: iPhone and Mac

One SwiftUI project, two apps. The agent's proactive messages (checks, reminders, heartbeat notes) arrive as
push notifications; the chat view streams replies and stays in sync across devices through the server's live
event feed (`GET /api/events`). Phone calls are separate: they come from the agent's Twilio number.

Requirements: Xcode 16+, a **paid** Apple Developer account (push notifications are not available on the free
tier), `brew install xcodegen`.

## Mac first, no Apple account needed

The Mac app does not need Apple push to notify you: while it runs (it stays in the menu bar), the server's live
feed delivers every message and the app posts a system notification whenever the chat is not in front. So:

```
cd apps/Atlas && ./build.sh          # xcodegen + build + sign with your Apple Development certificate + relaunch
cp -R build/Build/Products/Release/Atlas.app /Applications/
defaults write com.elityre.atlas baseURL "http://127.0.0.1:8800"
defaults write com.elityre.atlas token "<EA_WEB_TOKEN>"      # moved into the Keychain on first launch
open /Applications/Atlas.app                                  # click Allow on the notifications prompt
python -m ea notify "hello from the terminal"                 # should appear in the app, and as a banner if it is not in front
```
Add it to System Settings → General → Login Items so it is always running.

Signing matters even without a paid account: macOS grants notification permission only to an app whose code
signature is stable across launches. Ad-hoc signing (`codesign -s -`) changes it on every build and macOS then
refuses notifications (`UNErrorDomain error 1`). `build.sh` signs with the free "Apple Development" certificate
Xcode created when you signed in with your Apple ID (Xcode → Settings → Accounts). Apple push (below) is what adds
delivery when the app is closed, and is required for the iPhone.

## One-time setup for Apple push (iPhone, or Mac with the app closed)

1. **Apple Developer portal** (developer.apple.com → Certificates, Identifiers & Profiles)
   - *Identifiers → App IDs → +*: bundle id `com.elityre.atlas` (explicit), tick **Push Notifications**.
     Change `PRODUCT_BUNDLE_IDENTIFIER` in `project.yml` and `channels.apns_bundle_id` in the server's
     `config.toml` if you pick another id.
   - *Keys → +*: name it "Atlas APNs", tick **Apple Push Notifications service (APNs)**, download the
     `AuthKey_XXXXXXXXXX.p8` (one chance to download), note the **Key ID**. The **Team ID** is under Membership.
2. **Server** (`data/.env` of the agent):
   ```
   APNS_KEY_PATH=AuthKey_XXXXXXXXXX.p8     # the file goes in data/
   APNS_KEY_ID=XXXXXXXXXX
   APNS_TEAM_ID=YYYYYYYYYY
   ```
   Restart `python -m ea serve`; `python -m ea check` should say `push (iPhone/Mac apps): configured`.
3. **Signing**: the team ID is in `project.yml` (`DEVELOPMENT_TEAM: DW72928GK6`, Eli's paid team, the same one the
   Flutter apps use). Automatic signing registers the App ID and profiles; no Xcode UI needed.
4. **Install on the iPhone** (cable or same Wi-Fi, Developer Mode on, phone awake):
   ```
   ./build-ios.sh             # Release build, push entitlement on, installs over Wi-Fi with devicectl
   ./build-ios.sh --no-push   # same without aps-environment: installs even before the push App ID exists
   ```
   Installing over an existing copy is an update; settings and the token survive. First launch: the Settings sheet
   appears. Instead of typing, scan a QR of `atlas://setup?url=http://<your-mac>.local:8800&token=<EA_WEB_TOKEN>`
   with the Camera app (generate it with any QR tool; never commit the image). The app fills both fields and
   connects. The Mac app understands the same link.
   The status line should read `registered (sandbox)`. *Send test notification* proves the whole path.
   - Any build installed by Xcode or `build-ios.sh` (Release included) carries a development provisioning profile
     and so gets APNs **sandbox** tokens; the app reads `aps-environment` from its embedded profile and registers
     accordingly. A TestFlight/App Store build registers as **production**. The picker in Settings overrides it.
   - Away from home the server URL has to be the public one (`channels.public_base_url`, e.g. Tailscale Funnel).
     On the home network `http://<your-mac>.local:8800` works (local networking is allowed in the Info.plist).
5. **Run on the Mac**: destination *My Mac*, ⌘R. The default server URL is `http://127.0.0.1:8800`. The app
   also sits in the menu bar (✦) with the last messages and a composer. To keep it running: *Product →
   Archive → Distribute → Copy App*, drop it in /Applications, add to Login Items.

Debug installs from Xcode expire after a year with a paid account (7 days on a free one). TestFlight builds
last 90 days.

## iPhone

Same app, `ChatView` only (no thinking or workspace panes yet). Reaches the server at `http://<mac>.local:8800` on
the home network; away from home it needs the public URL (Tailscale Funnel, deferred). Without the APNs key the
phone gets replies only while the app is open. One-button dictation into the agent is a Shortcut, not the app:
see `../../ios/SHORTCUT.md`.

**Away from home: Tailscale** (2026-11-11). The Mac runs the App Store Tailscale app (the Homebrew cask needs an
admin password for its installer, so it could not be scripted); its tailnet name is
`elis-macbook-pro.tail5a9488.ts.net` (100.115.15.76). The phone runs the Tailscale app signed in to the same
account, and the Atlas server URL on the phone is `http://elis-macbook-pro.tail5a9488.ts.net:8800`, which works on
any Wi-Fi or cellular. Nothing is exposed publicly (no Funnel). Two Info.plist pieces make this work: the ATS
exception for `ts.net` (plain http is allowed there; the link is WireGuard-encrypted) and
`NSLocalNetworkUsageDescription` for the `.local` fallback at home.

**Offline** (Eli, 2026-11-11): the last 300 messages are cached in the app's Application Support folder
(`Atlas/messages.json`, written 0.8 s after any change), so the chat opens with its history without a connection.
A message sent while the server is unreachable (any `NSURLErrorDomain` error) stays on screen with a small
"waiting for a connection" label and is sent, in order, when the live feed reconnects, the app comes to the
foreground, or a 30 s timer fires; the reply then streams in as usual. Server-side errors (HTTP 4xx/5xx) are not
queued; they show as errors as before. Installed on Eli's iPhone 16e on 2026-11-11, first with `--no-push`, then (after the PLA was accepted) the full build
with the push entitlement; the server still needs the APNs key before anything is delivered.
Verified: the cache file is written on the Mac; the queue path compiles but was not exercised end to end
(the Mac test needs Accessibility rights for scripted typing). Test on the phone: Airplane Mode, send, turn it off.

**Sent text stayed in the field** (Eli, 2026-11-12): on iOS the vertical-axis `TextField` writes its old text
back into the binding right after `onSubmit`, undoing the clear in `ChatStore.send()`. The store clears the draft
a second time on the next main-queue pass when it still equals the sent text. The Mac composer (a `TextEditor`)
never had this.

## Thinking pane (Mac)

Beside the chat, an optional pane shows the agent's chain of thought for every turn, yours and its own
scheduled ones: the trigger, the model's reasoning summary (italic), tool calls with inputs (click to expand),
results, and the decision. Toggle it with the brain button in the toolbar, View → Show/Hide Thinking, or ⌘⇧T;
the choice is remembered. It needs `llm.thinking_display = "summarized"` on the server (the default) to show
reasoning text; with `"omitted"` only tool calls appear.

## Workspace pane (Mac)

Shared Markdown pages both you and the agent edit, apart from the conversation: toolbar button or ⌘⇧W. The
whiteboard is the default page; the picker in the pane header switches to other shared documents, and the
menu creates or deletes one. The editor is Literal (Eli's own): rendered Markdown with the markup hidden, edited
in place; ```mermaid fences are drawn as diagrams under the text. It autosaves shortly after you stop typing,
the agent's edits appear live, and if both sides changed the page a banner lets you load theirs or keep yours.
Files live in `data/workspace/` on the server, so any editor works too; every version of every page is kept.
Updating the editor: build Literal (`npm run build`), copy `out/renderer.js` and `src/styles.css` into
`Atlas/Resources/literal/`, run `./build.sh`.

## Notifications that stay

When the agent sends something and the chat is not in front, the app shows its own notice: a small floating
panel at the top-right of the screen that stays until you click it (click = open or activate the chat), or
until the chat comes to the front on its own. The system notification is still posted as well; macOS shows
those as banners that fade unless you set Atlas to "Alerts" in System Settings → Notifications. Clicking a
system notification also opens the chat, creating the window if it was closed (via the `atlas://open` URL).

## Images

Paste an image (⌘V) or drag image files or images onto the chat; they appear as thumbnails above the composer and
go with the next message (downscaled to 1568 px). The agent sees them, they show in your bubble, and the history
reloads them from the server.

## Sending while the agent is working

Send any time. If the agent is mid-turn, the message is handed to that turn (the agent sees it at its next step
on the API engine) or starts the next turn as soon as the current one ends; the reply arrives on the live feed.

## Behaviour

- Notification while the chat is on screen: no banner (the message already appeared live). Otherwise the
  usual banner + sound; tapping it opens the thread. Replies to your own messages are also pushed, collapsed,
  to the device that asked, so a phone that locked while the agent thought still gets the answer.
- Everything one device sends shows up on the others within a second (event feed), and on reconnect the thread
  is reloaded from the server, which is the source of truth.
- Token in the Keychain, server URL / device name / push environment in UserDefaults. Nothing else is stored.

## Files

`project.yml` (xcodegen spec; the .xcodeproj is generated, not committed) · `AtlasApp.swift` (scenes) ·
`AppDelegate.swift` (APNs callbacks, notification delegate) · `PushManager.swift` · `AppConfig.swift`
(settings + Keychain) · `API.swift` (server client) · `ChatStore.swift` (state, streaming, live feed) ·
`ChatView.swift` · `SettingsView.swift` · `MenuBarView.swift` (macOS) · `Atlas-iOS.entitlements`,
`Atlas-macOS.entitlements` (aps-environment; sandbox + network client on the Mac).

Server side: `ea/channels/push.py` (APNs, device registry), `ea/channels/events.py` (live feed),
`POST /api/devices`, `GET /api/events`, `POST /api/push/test`, and `python -m ea push-test`.

## Gotchas learned

- `xcodebuild ... -allowProvisioningUpdates` fails with "PLA Update available: You currently don't have access to
  this membership resource" whenever Apple has published a new Program License Agreement: someone has to accept it
  at developer.apple.com first. Until then the push App ID cannot be created; `build-ios.sh --no-push` still builds
  and installs with the team's wildcard profile (2026-11-11).
- A conditional build setting cannot be passed on the xcodebuild command line (`"X[sdk=iphoneos*]=…"` is read as
  a file name); put it in an `-xcconfig` file instead, which is what `build-ios.sh --no-push` does.
- `xcrun devicectl ... install` error 4016 ("not able to fulfill the requested usage assertion") just means the
  phone is asleep or off the network; `devicectl list devices` shows it as `unavailable`. Wake it or plug it in.
- The phone never reached the Mac after the first install (no request in the server log at all): the Info.plist
  lacked `NSLocalNetworkUsageDescription`. On iOS 14+ a `.local` name or a private IP is "local network" use and is
  refused silently without that key; with it the user gets a one-time prompt. Added 2026-11-11 (`project.yml`).
- Apple answered `400 BadDeviceToken` to the first real push (2026-11-11): the app had guessed "production" because
  it was a Release build, but a devicectl-installed build has a development profile and therefore sandbox tokens.
  The environment now comes from the embedded provisioning profile, not from the build configuration. The server
  forgets a device on BadDeviceToken, so the app must relaunch to register again.
- App Transport Security treats a Tailscale address (100.x or `*.ts.net`) as a remote host, so plain http to it is
  refused unless `NSExceptionDomains` lists `ts.net` with `NSExceptionAllowsInsecureHTTPLoads`; "local networking"
  only covers `.local` names and private ranges.
- `osascript ... keystroke` is refused ("not allowed to send keystrokes", error 1002) unless the terminal has
  Accessibility permission, so a UI-level test of the Mac app cannot be scripted from Claude Code without it.
- Anything shared between the Mac and iPhone views must compile on both; a helper left inside `#if os(macOS)` but
  used outside it (`attachmentStrip`, 2026-11-11) only shows up when the iOS build runs. Build both before
  calling a UI change done.
