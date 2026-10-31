# Contrast fix for the Streamlit build

Problem in the current build: secondary text is light grey (~#a8a39a) at 11–13 px on sand, so it blends into the background. The Play button text is invisible (navy on navy), Step and To end look disabled, chart labels are pale, and "-&gt;" is double-escaped.

## Rules
- Text colours, never lighter than these on `#f6f2ea` / `#efe9dc` / `#fff`:
  - Headings and values: `#1d1f22`
  - Body: `#2e3135`
  - Secondary (hints, captions, labels): `#4a4d52` (≈ 7.5:1). **Delete every lighter grey.**
- Minimum sizes: body 15 px, secondary 14 px, small labels 13 px. Nothing under 13 px except the uppercase group labels (12 px / 700).
- No opacity or `color-mix` on text.

## Global CSS (inject once in `app.py`)
```python
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
html, body, [class*="st-"], p, li, label, span { font-family: 'IBM Plex Sans', sans-serif; }
[data-testid="stAppViewContainer"] { background:#f6f2ea; color:#2e3135; }
[data-testid="stMainBlockContainer"] { max-width:1180px; padding-top:1.5rem; }
p, li { color:#2e3135; font-size:15px; line-height:1.55; }
h1, h2, h3 { color:#1d1f22 !important; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * , .stCaption { color:#4a4d52 !important; font-size:14px !important; }

/* sidebar nav */
[data-testid="stSidebar"] [role="radiogroup"] label { padding:8px 10px; border-radius:8px; }
[data-testid="stSidebar"] [role="radiogroup"] label > div:first-child { display:none; }   /* hide radio circle */
[data-testid="stSidebar"] [role="radiogroup"] label p { color:#1d1f22; font-size:15px; font-weight:500; }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) { background:#fff; }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) p { font-weight:700; }
[data-testid="stSidebar"] [role="radiogroup"] label:hover { background:#e6dfcf; }
/* hint line under each nav item: render with this class */
.nav-hint { color:#4a4d52 !important; font-size:13px !important; }
.nav-group { color:#4a4d52; font-size:12px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; }

/* metrics */
[data-testid="stMetricLabel"] p { color:#2e3135 !important; font-size:14px !important; font-weight:600; }
[data-testid="stMetricValue"] { color:#1d1f22 !important; font-variant-numeric:tabular-nums; }
[data-testid="stMetricDelta"] { color:#2e3135 !important; background:#ebe4d4 !important; }
[data-testid="stMetricDelta"] svg { fill:#2e3135 !important; }

/* buttons */
.stButton button { font-size:14px; font-weight:600; border-radius:8px; }
.stButton button[kind="primary"], .stButton button[kind="primary"] * { background:#1b2f4a !important; color:#fff !important; border-color:#1b2f4a !important; }
.stButton button[kind="secondary"], .stButton button[kind="secondary"] * { background:#fff !important; color:#1d1f22 !important; border-color:#b9b2a3 !important; }
.stButton button:disabled, .stButton button:disabled * { color:#4a4d52 !important; background:#ebe4d4 !important; }

/* status pills from st.markdown */
.pill { font-size:13px; font-weight:600; color:#1d1f22; background:#fff; border:1px solid #d6cfbf; }
</style>""", unsafe_allow_html=True)
```

## Plotly (every figure)
```python
fig.update_layout(
    font=dict(family="IBM Plex Sans", size=14, color="#2e3135"),
    paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
    xaxis=dict(tickfont=dict(size=13, color="#2e3135"), gridcolor="#e4ddcf", linecolor="#b9b2a3", title_font=dict(size=14, color="#2e3135")),
    yaxis=dict(tickfont=dict(size=13, color="#2e3135"), gridcolor="#e4ddcf"),
    legend=dict(font=dict(size=13, color="#1d1f22")),
)
```
Key-moments lane labels ("first symptom", "cause"…) are y-axis ticks, so they get size 13 and colour #2e3135 from the above. Wrap the chart in a white card (`st.container(border=True)`).

## Specific fixes seen in the screenshot
1. **Play button text invisible**: it's the primary button; the CSS above forces white text.
2. **Step / To end look disabled**: secondary style above. Only grey them out when actually disabled.
3. **Sidebar hints** ("What happened" etc.): #4a4d52, 13 px. Hide the radio circles.
4. **Header subtitle** ("Incident scn_0003…", "Mode: Live…"): 14 px #4a4d52. Drop the duplicated "Record: Live: rules only."
5. **Energy explanation paragraph** (long pale sentence under the metrics): move into `st.expander("How is energy below expected measured?")`, body text 15 px #2e3135.
6. **"-&gt;" in key moments**: the text is escaped twice. Escape plant free text once (`html.escape`) and write arrows as "→" in `plain.py`, not "->".
7. **Metric delta pills** ("↑ 3 fixed, 0 to a human"): CSS above. The arrow means "up" here, which is wrong for "0 to a human"; use `delta_arrow="off"` (or render a caption instead).
8. **"Turn 9" buttons**: label them "Open step" (keep the keys); they are the action, and the time is already on the left.
9. **Page width**: content stretches to ~2200 px; `max-width:1180px` above keeps lines readable.
10. **Shortfall "17% / -1%"**: write it as "17 % → −1 %" with a caption "worst hour → last hour".
