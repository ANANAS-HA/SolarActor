# Plant tab: dashboard-style panels (replaces the Plant section of README)

Look: white panels, 1px #d9d2c3 border, 6px radius, panel header (title 15px/600 · unit 13px #4a4d52 · takeaway sentence right-aligned 14px/600 in state colour). All text ≥13px, #2e3135 or darker.

## Order
1. **Stat row** (4 panels, auto-fit min 200px): label 13px/600 · value 30px/600 tabular · state line (dot + words) · full-bleed sparkline 36px at the bottom.
   Plant output now · Energy below expected (MWh so far) · Alarms so far · Waiting for your approval.
2. **Sun and wind** panel.
3. **Plant output against expected** panel.
4. **Plant map** (flex 3) + **equipment column** (flex 1, wraps under 820px).

## Time-series panels (Plotly config)
- `hovermode="x unified"`, spikelines on (`xaxis.showspikes=True, spikemode="across", spikethickness=1, spikecolor="#1d1f22"`), hoverlabel bg #1b2f4a, white 13px text.
- Shared crosshair feel: same x-range 06:00–18:00 on both panels, ticks every 2 h, mono 13px.
- Gridlines #ece6d9, horizontal only. No Plotly legend; render a **legend table** under the chart: Series (line sample + bold name + plain-words meaning) | Now | Min | Max, right-aligned tabular numbers.
- **Annotation band** (`add_vrect`, fill #f6e3c1, no line) "Dust storm, trackers stowed", label 13px/600 #6b3d00 at top-left.
- **Event markers** (`add_vline` dotted navy) with a numbered navy circle at the top; numbers explained in a row under the legend: "① 09:45 Cleaning ordered".
- **Threshold** (`add_hline` dashed #b3261e, 1.5px) "Stow limit 15 m/s" label right.
- Playback cursor: `add_vline` solid #e8a33d 2px at sim time.
- Sun and wind: sun height = filled area rgba(232,163,61,.22) with #b06d12 line (left axis 0–90°); wind = navy #1b2f4a 2.5px (right axis 0–25 m/s).
- Output: Actual navy solid 3px; Expected satellite #1f7a45 dashed; Expected sun sensor #b06d12 dotted (stops when the sensor is dropped). Takeaway: "Now 34.8 MW, 87 % of what the weather allows".

## Plant map (HTML, not Plotly)
- Row label column 104px: "Transformer 1" 14px/600, "Group A" 13px.
- Tiles: white, 1px #d9d2c3, left-aligned. Name 14px/600 ("Inverter 01"; raw INV-01 only under Show data) · MW 22px/600 · "101 % of peers" 13px · state dot + word 13px/600.
- Non-normal tile only: 2px state-colour border + light tint (#fdf4e3 warn), plus a "Review" button when it needs approval. Normal tiles are NOT green-filled (that killed contrast).
- Header right: "Plant-wide: Cleaning ordered".
- Equipment cards (Grid connection, Substation meter, Weather station): header name + state; body = key/value rows (14px, key #2e3135, value 600 #1d1f22 right-aligned). Abnormal card gets a 2px state border.
- Drop the fence labels and connector lines; grouping by transformer row already says it.
