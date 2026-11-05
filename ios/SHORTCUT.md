# iPhone: one button to talk to the agent

Two ways to reach the agent from the phone with a single action. Both use the agent's HTTPS endpoint
`POST /api/message` with a bearer token (`EA_WEB_TOKEN`) and get the reply back as plain text, which the
Shortcut speaks and shows. Dictation happens on the phone, so no audio is uploaded.

## A. The Shortcut (works for the Action button and for Siri)

Shortcuts app → + → name it exactly the agent's name, e.g. **Atlas** (so Siri's phrase is "Atlas").

Actions, in order:

1. **Dictate Text**  (Language: your language; "Stop Listening: After Pause")
2. **Get Contents of URL**
   - URL: `https://<your-public-base-url>/api/message`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <EA_WEB_TOKEN>`
   - Request Body: `JSON` → add field `text` = *Dictated Text* (the variable from step 1),
     and field `channel` = `shortcut`
3. **Get Dictionary Value** — key `reply`, from *Contents of URL*
4. **Show Result** — *Dictionary Value*  (and/or **Speak Text** — *Dictionary Value*, with
   "Wait Until Finished" on, so it reads the answer aloud when you cannot look at the screen)

Optional step 5: **Ask for Input** "Reply?" → loop back by running the same shortcut (Shortcuts →
"Run Shortcut" with input), which gives a back-and-forth without touching the screen.

Tips
- In the shortcut's settings turn **Show in Share Sheet** off and **Allow Running When Locked** on.
- The first run asks permission to contact your server's domain; allow "Always".
- Give the shortcut an icon and add it to the Home Screen or a widget as a second tap target.

## B. One physical button: the Action button (iPhone 15 Pro and later)

Settings → Action Button → scroll to **Shortcut** → choose **Atlas**. Holding the Action button now
starts dictation straight into the agent, and the reply is spoken back.

On phones without an Action button: Settings → Accessibility → Touch → **Back Tap** → Double Tap →
**Atlas**. Two taps on the back of the phone does the same.

## C. Saying it to Siri

- "Hey Siri, **Atlas**" → the shortcut runs → dictate the message after the tone.
- You can also say "**Atlas** what's on my schedule today" in one breath on iOS 17+: Siri runs the
  shortcut and passes the trailing words as its input. To use that input instead of a fresh dictation,
  add at the top of the shortcut: **If** *Shortcut Input* has any value → use it as the text → **Otherwise**
  → Dictate Text. (Shortcuts → the shortcut's "i" → "Receive … input from Quick Actions / Siri".)
- Siri's own phrase is the shortcut's name, so renaming the shortcut renames the command.

## Replies

The server returns `{"reply": "..."}`. Replies are written for a phone screen (short, plain text) and
the voice-mode rules apply only on phone calls, so if you mostly use Speak Text you may want to tell the
agent "when I message you from the Shortcut, answer in a sentence or two" and it will remember.

## Phone calls (live conversation)

Call the Twilio number from your phone (save it as a contact named after the agent). The agent answers,
you speak, it replies; say "goodbye" to hang up. Only numbers in `twilio_allowed_callers` get through.
The agent can also call you (it has a `call_user` ability when Twilio is configured) for something it
judges urgent during a scheduled check.
