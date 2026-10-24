# SehatAI chatbot demo script

A scripted conversation that shows what the assistant can do: natural chat, follow-up questions, corrections, using your
blood-report values and health intake, safety boundaries, emotional support, the emergency flow, other languages, and the
diet assistant. Type each prompt exactly as written (or in your own words; the bot is not keyword-based).

## Before you start (5 minutes)

1. **Use an adult profile.** The date of birth on the Profile page must give an age of 18 or more. Under 18, the Infermedica
   plan refuses the request and the bot can only suggest a Pediatrician.
2. **Health intake** (sidebar, "Health intake"): add one condition (for example Asthma, 2015) and one allergy (for example
   Penicillin, rash). Set the **emergency contact to your own email**. Pressing "Notify someone" later sends a real email to
   that address if email is configured.
3. **Upload a blood report** (sidebar "Reports", "+ Add report"). Use `docs/demo/sample_lab_report.pdf` (fake patient; known
   values: hemoglobin 10.8 LOW, WBC 11.8 HIGH, platelets 250, fasting glucose 112 HIGH, cholesterol 185, creatinine 0.8), or
   your own report. Wait until it appears as a card (up to ~3 minutes).
4. Open **AI Assistant**, make sure **Symptoms** is selected, and press **New session**.
5. **Run through it once before showing anyone.** Each reply takes 30 to 120 seconds. The whole demo is about 30 minutes.

## Act 1. A normal conversation that uses your blood report

| # | Type | What it shows |
|---|---|---|
| 1 | `hey, I've been feeling really worn out lately and a bit dizzy when I stand up` | Understands casual language; asks a natural follow-up (how long) |
| 2 | `since last week, so like 5 days` | Remembers the symptoms; asks how severe |
| 3 | `I'd say 6 out of 10, it comes and goes` | Records duration and severity per symptom |
| 4 | `no fever, but I have been getting headaches too` | Adds a new symptom mid-conversation; asks about it |
| 5 | `actually the headaches only started yesterday` | Accepts a correction |
| 6 | `the dizziness is gone now actually` | Removes a symptom, says what it removed and what is left |
| 7 | `no that's all` | Moves to the recommendation |

**Expected:** a specialist recommendation (chosen by Infermedica, written in friendly language), with a phone-or-in-person
suggestion. It mentions your intake where relevant (for example your asthma) and **can quote your blood-report values**,
such as low hemoglobin or high fasting glucose.

> Honest note: the bot only brings in lab values when Infermedica suggests blood tests that match your report. Tiredness
> and dizziness are good symptoms to try, but it is not guaranteed on every run. It does not summarise a report on demand;
> the Reports page has an AI summary for that.

## Act 2. It knows its limits

| # | Type | Expected |
|---|---|---|
| 8 | `so what do you think I have?` | Politely declines to diagnose and points you to a doctor |
| 9 | `what's the weather like in Lahore today?` | Redirects: it only helps with health symptoms |
| 10 | `my son has a fever and a cough` | Explains it only helps with your own symptoms and profile, and suggests his doctor |

## Act 3. Emotional support

Press **New session**, then:

| # | Type | Expected |
|---|---|---|
| 11 | `honestly I've been feeling really low and anxious lately` | A warm check-in question, not a canned referral |
| 12 | `yeah it's been hard` | Supportive reply, suggests a counselor or someone you trust |
| 13 | `also I keep getting headaches` | Moves to the physical side and asks about the headaches |

## Act 4. Another language

New session:

| # | Type | Expected |
|---|---|---|
| 14 | `mujhe sar dard hai aur bukhar bhi hai` | Replies in Roman Urdu and asks since when |
| 15 | `2 din se` | Continues in Roman Urdu |

## Act 5. Emergency (do this last: it locks the chat)

New session:

| # | Type | Expected |
|---|---|---|
| 16 | `I suddenly have chest pain and I can't breathe` | An emergency notice straight away, with two buttons |
| 17 | Press **Notify someone** | Asks "Alert <name>?"; confirm to email your emergency contact (use your own email) |
| 18 | Press **Continue with this chat** | Carries on with follow-up questions and repeats the emergency warning in the final answer |

## Act 6. Diet assistant (uses your blood report too)

Press **Diet**, then **New session**:

| # | Type | Expected |
|---|---|---|
| 19 | `Can you suggest a simple dinner that suits my latest blood report?` | A meal idea that takes your labs (for example high fasting glucose, low hemoglobin), allergies and conditions into account |
| 20 | `what about breakfast, I'm vegetarian` | Adapts to the new preference |

> Honest note: the code reads your latest labs, medications and allergies for this assistant. I tested that it replies about
> food, but I have not checked which lab values it quotes in a reply. Look at the first answer before you present.

## Things to avoid in a live demo

- **"Can I take <a medicine>?"** The bot does not answer medicine questions and currently ignores them, even when you are
  allergic to the medicine. This is a known gap.
- **Photos or scans of reports** were not tested, only a PDF with real text.
- Do not expect fast replies. Narrate while it works.
