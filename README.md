# recallbiz

Telegram-native business card scanner + personal CRM.

**Domain:** recallbiz.xyz (purchased 2026-06-17, Namecheap/Porkbun)
**Bot handle:** Nick has it (will share when needed)
**Sketch day:** Sunday 2026-06-21

## Core pitch
Scan a business card (photo, Telegram QR, plain paper) → OCR/parse → store in personal DB → query via chat ("who did I meet at Token2049?") → share via native Telegram contact card. Built for the "I have 500 telegrams and no idea who anyone is" problem.

## Pricing (decided 2026-06-17)
| Tier | Price | Cards | M3 features | Notes |
|---|---|---|---|---|
| **Free** | $0 | 10 cards | Manual tags, basic search, share | Validates the loop |
| **Pro** | $9.99/mo or $99/yr | Unlimited | M3 follow-up drafts, batch mode, trip mode, deep-link events, advanced search | Core revenue |
| **Team** | $49/mo | Unlimited | + shared team contacts, admin | For sales teams / VCs |

## Key feature pillars (locked in)
1. **Telegram QR scan** — forward QR image → bot calls `getChat` → quick chips → save
2. **Image OCR** — paper cards via M3 vision (cheaper than dedicated OCR APIs)
3. **Trip mode / active event context** — "I'm at Token2049 Singapore Sep 18-20" → all saves auto-tag for that window
4. **Deep link event context** — `t.me/recallbiz_bot?start=event_TOKEN2049` from event organizers
5. **M3 follow-up drafts** — context-aware ("Hey John, great chat at Token2049 about L2 scaling...")
6. **Batch mode** (Pro) — "draft follow-ups for all 12 VCs from Token2049" = M3 batch = paid feature
7. **Native vCard share** — `sendContact` API, zero install for recipient = viral loop
8. **TON Foundation grant** — TON wallet integration + TON-holder discount + TON community promotion

## Use case (Nick's pain, real)
Goes to crypto conferences → everyone shares Telegram QR → ends up with 500+ contacts → no idea who anyone is or where they met → "what does that guy from Singapore do?" → 30 minutes of scrolling. recallbiz fixes this.

## Cost model (Nick asked for cheap)
- Telegram API: free
- Storage: ~1KB per contact
- M3 calls: ~$0.001 each (vision parse, follow-up draft, query)
- 1k users × 50 scans/mo = ~$50/mo M3 cost
- Hosting: Railway free tier or Oracle Cloud free tier
- Total: <$100/mo at 1k active users

## Viral loop math
User A scans 50 cards/year → shares 30 via native vCard → 30 recipients see "via recallbiz" in phone → 10% convert = 3 new users per A per year. At 1k active As = 3k signups/year with zero marketing spend.

## TON Foundation grant angle (to pursue)
- Build TON wallet integration → users pay for Pro with TON
- Apply to TON Foundation grants program
- They'd promote us to TON community (millions of crypto-native users)
- Give TON holders 30% off Pro (verify via wallet signature)
- Fits TON's "everyday crypto utility" narrative

## SEO keywords to own (for recallbiz.xyz landing page)
Primary:
- telegram business card scanner
- remember contacts from conference
- telegram contact organizer
- scan telegram qr code to save contact
- crypto event contacts

Long-tail:
- networking contacts CRM
- business card scanner free
- save telegram contact with note
- vcf card organizer
- who did i meet at conference

## What goes in Sunday sketch (2026-06-21)
- [ ] Full SQLite schema (contacts, events, users, shares, referrals)
- [ ] Bot flow diagrams (scan / save / search / share)
- [ ] M3 prompts (parse card, generate follow-up, NLU on search)
- [ ] TON grant application template
- [ ] Competitor pricing deep-dive (this file is the starter)
- [ ] SEO landing page outline (recallbiz.xyz)
- [ ] MVP 4-week build plan
- [ ] First 100 users launch playbook
- [ ] Privacy/GDPR stance (delete in 1 tap)
- [ ] Viral loop math + content for promotion

## Open questions
- What's the bot handle? (Nick has it)
- Build solo first, then TON grant, or apply for grant first?
- v1 = OCR + Telegram QR only, or also LinkedIn / paper card variants?
- Telegram bot payment via TON, Stripe, or both?