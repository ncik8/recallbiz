# TRCE Tester Guide (formerly RecallBiz)

Welcome! You're helping Nick test TRCE — a Telegram bot that scans business cards and saves the contact for you. This takes about **10 minutes**.

---

## What you'll be testing

1. Signing up with your email
2. Scanning a business card photo
3. Scanning a QR code
4. Searching your contacts
5. Tagging and adding notes

---

## Step 1: Start the bot

1. Open Telegram on your phone
2. Search for `@trceiobot`
3. Tap **Start** (or send `/start`)

You should see a welcome message from the bot ("Welcome to TRCE — Trace AI").

---

## Step 2: Sign up (required)

The bot requires email signup before you can save contacts. This protects your data and prevents spam.

In Telegram, send:

```
/signup your-real-email@example.com
```

Check your email inbox (and spam folder) for a message from TRCE. Click the magic link inside. It'll take you back to the bot automatically.

**You should see:** *"Email confirmed: your@email.com — You're signed up. Free plan includes 10 contacts."*

---

## Step 3: Scan a business card

1. Find any business card (physical or a photo of one)
2. **Take a clear, well-lit photo** of it
3. Send the photo to `@RecallBizBot` in Telegram (as a photo, not a file)

The bot will:
- Read the card with AI vision
- Show you what it extracted (name, company, email, phone)
- Ask: *"Save this contact?"*

Reply **Yes** to save.

---

## Step 4: Scan a QR code

1. Find any QR code (Telegram username QR, website URL, vCard, etc.)
2. Take a screenshot or photo of it
3. Send it to the bot

The bot will detect the QR and save what's in it.

---

## Step 5: List and search your contacts

```
/list
```

Shows your last 10 contacts. To search:

```
/find Vitalik
```

Searches name, company, notes — anywhere.

---

## Step 6: Try the trip mode (optional)

If you're at a conference or event and want to auto-tag everyone you save:

```
/trip set Token2049
```

Now every contact you save will be tagged with `event:Token2049`.

```
/trip
```

Shows your current trip + how many contacts you've saved to it.

---

## What's broken on purpose (please don't report)

- **/stats command** — that's founder-only, you'll see "Unknown command." if you try it
- **Pro/Team plans** — not active in this build, only the free tier works
- **Stripe payment** — not wired up yet

---

## How to report bugs

When something breaks or feels wrong, send Nick (or me, Henry) a Telegram message with:

1. **What you were trying to do** (e.g. "scan a business card")
2. **What happened** (screenshot of error if any)
3. **What you expected** to happen

Or just describe the bug — I'll figure out what you meant.

---

## What to test specifically

If you want to be thorough, try these:

- [ ] Sign up with email
- [ ] Save a contact from a business card photo
- [ ] Save a contact from a Telegram contact share (open a contact in Telegram, share it)
- [ ] Save a contact from a QR code
- [ ] Use `/find` to search by name
- [ ] Use `/find` to search by company name
- [ ] Use `/list` to see your contacts
- [ ] Start a trip with `/trip set <event>`
- [ ] Save a contact while in a trip (should auto-tag)
- [ ] Try to save an 11th contact (should hit free-tier limit and prompt upgrade)

---

Thanks for testing! 🐢
