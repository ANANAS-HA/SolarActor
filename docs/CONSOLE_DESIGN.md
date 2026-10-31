# Console design plan (lane D, 2026-10-31)

Who reads the console: hackathon judges and a plant manager, neither an expert in agentic systems or solar
plants. Its primary job: show, in one pass, what went wrong at the plant, what Raqeeb did about it, and what
still needs a person. Everything below follows from that.

## Tokens

Colour (base, 6 named values):

| Name | Hex | Use |
|---|---|---|
| Sky haze (page) | `#f2f5f9` | page background, pale and cool |
| Surface | `#ffffff` | cards, sidebar, tables |
| Deep navy (ink) | `#13293f` | all text and headings |
| Slate (muted) | `#51657a` | captions, secondary labels |
| Control blue (accent) | `#1d5fb4` | links, primary buttons, the "actual output" series |
| Rule | `#d6dee8` | borders, dividers |

Semantic states, each a text colour on a light fill, every pair at least 4.5:1, and always with a word or
icon beside the colour (never colour alone):

| State | Ink | Fill |
|---|---|---|
| healthy / recovered | `#116b3f` | `#def3e6` |
| attention / finding | `#8a4b00` | `#fff0cc` |
| critical / escalated | `#a8251c` | `#fde2df` |
| no data | `#4a5563` | `#e9edf2` (hatched tiles on the map) |
| needs a human | `#5b3aa6` | `#ece5fa` |

Chart series keep a fixed order: actual output control blue, weather reference rust `#c2571a`, on-site sensor
teal `#117a65`; the clear-sky line is muted. Measured on the final palette: the lowest text pair is 5.1:1.

Type: one family (Streamlit's bundled sans serif, Source Sans), one scale: title 1.5 rem / 600, section 1.25
rem / 600, card headline 1.05 rem / 600, body 1 rem / 400, caption 0.85 rem muted. Sentence case everywhere;
no all-caps labels; no numbered markers unless the content is a sequence (the Trace is one: "Turn 7, 09:15").

Layout (left aligned, built for a 1366 px laptop):

```
+-- Raqeeb  plant recovery console      Plant  Mode  Simulated clock  Model ---------------+
| ▌ 09:15  Transformer trip handed to a technician  [Needs your decision: 1] [Handed: 1]  |   <- notification strip
| [approval card, only while an action waits]                                             |
| Plant output now | Alarms so far | Inverters reporting | Waiting for your approval      |
| Summary | Trace | Plant | Agent | Report | Answer key | Results | Ask Raqeeb              |
| two-line intro                                                                          |
| ... tab content ...                                                                     |
```

Principles:

1. Words first, numbers second, identifiers last. Every fault, device, tool, verdict, action and gate
   decision goes through `raqeeb/ui/plain.py`. Raw values stay one click away behind "Show data" on each card.
2. One memorable element: the notification strip. It is the only thing that moves (a pulsing dot while a
   decision is needed; still under `prefers-reduced-motion`) and the only coloured left rule on the page.
3. Quiet surfaces. Cards are white on the pale page with a 1 px rule, no shadows, no gradients; the status
   colour is carried by a chip with a word, or by a thin left rule on a finding.
4. Empty sections say what would appear there, in one line, instead of showing an empty table.

## Review against the generic defaults

- Not a cream page with a terracotta accent, not a dark page with an acid accent: the brief asks for a light
  control room; the page is cool (blue-grey) and the accent is a working-blue, chosen because the plant's
  own charts already use it for "actual output" so the accent and the data agree.
- No ALL-CAPS eyebrow labels, no middle-dot meta strings (the old captions joined everything with " · ";
  rewritten as sentences), no identical shadowed cards: the Trace cards share a border but carry
  different content blocks (headline, findings with a left rule, decisions as sentences).
- No arrow glyphs appended to buttons; button text says what happens ("Play the storm", "Approve").
- Changed on review: the first draft gave each tab a big metric row; the metrics now appear once, above the
  tabs, so the tabs start with their two-line intro and their content.

## Where it is applied

- `.streamlit/config.toml` `[theme]`: base colours, radius, borders, one font.
- `raqeeb/ui/app.py`: `TOKENS`, `SEMANTIC`, `SERIES`, the `CSS` block (header, strip, chips, headline,
  finding rule), `chip()`.
- `raqeeb/ui/plantmap.py` keeps its own tile palette (states with icon and word), already in the same family.
