"""Regenerate the vector project overview diagrams without external packages."""
from pathlib import Path
from html import escape

OUT=Path(__file__).resolve().parents[1]/'docs/assets'
OUT.mkdir(parents=True,exist_ok=True)

def box(x,y,w,h,title,lines,accent):
    body=''.join(f'<text x="{x+22}" y="{y+64+i*24}" class="body">{escape(line)}</text>' for i,line in enumerate(lines))
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="#102a3a" stroke="#30546a"/><rect x="{x}" y="{y}" width="7" height="{h}" rx="4" fill="{accent}"/><text x="{x+22}" y="{y+35}" class="box-title">{escape(title)}</text>{body}'

blocks=''.join([
 box(42,145,280,155,'01  STEREO CAMERA',['3840 × 1080 MJPEG','Single side-by-side frame','1920 × 1080 per eye'],'#43d6c8'),
 box(360,145,340,155,'02  VISUAL PERCEPTION',['SCRFD detection per eye','Stereo alignment + association','Shared visual bearing'],'#43d6c8'),
 box(42,390,280,155,'03  FOUR RAW MICS',['CH2 / CH3 · moving ears','CH1 / CH4 · fixed body','48 kHz · 16-bit WASAPI RAW'],'#80b9ff'),
 box(360,390,340,155,'04  ACOUSTIC DIRECTION',['Silero speech support','Reliable-band pairwise TDOA','Pose-aware conditional fusion'],'#80b9ff'),
 box(755,255,295,180,'05  TARGET SELECTION',['Visual-first tracking','Sound-sector association','Recency + switch protection','Fallback when uncertain'],'#e8b464'),
 box(1095,255,303,180,'06  HEAD CONTROL',['Persistent yaw destination','Bounded smooth trajectory','Position feedback + limits','Stop / fault / visual takeover'],'#e8b464'),
])
arrows='''<g fill="none" stroke-width="3" marker-end="url(#arrow)">
<path d="M322 220 H352" stroke="#43d6c8"/><path d="M322 465 H352" stroke="#80b9ff"/>
<path d="M700 222 C734 222 715 300 747 300" stroke="#43d6c8"/>
<path d="M700 468 C734 468 715 386 747 386" stroke="#80b9ff"/>
<path d="M1050 345 H1087" stroke="#e8b464"/>
<path d="M1247 435 V612 H525 V553" stroke="#7fa9bc" stroke-dasharray="10 8"/>
</g>'''
svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="760" viewBox="0 0 1440 760" role="img" aria-labelledby="title desc">
<title id="title">DeepSight-3D audio visual architecture</title><desc id="desc">Two perception branches feed visual-first selection and bounded head control. Motor feedback assists body microphone coordinate transformation.</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="#a4c4d3"/></marker></defs>
<style>text{{font-family:Inter,Arial,sans-serif}} .title{{font-size:34px;font-weight:700;fill:#edf8fb}} .sub{{font-size:17px;fill:#a7c1cd}} .box-title{{font-size:17px;font-weight:700;fill:#f1fbff}} .body{{font-size:15px;fill:#c0d1da}} .foot{{font-size:15px;fill:#9db9c6}}</style>
<rect width="1440" height="760" rx="28" fill="#071b28"/><text x="42" y="67" class="title">AUDIO + VISION → SELECT → TURN</text>
<text x="42" y="101" class="sub">Independent perception branches feed a visual-first selector; reliable audio assists.</text>
{blocks}{arrows}<text x="642" y="596" class="foot">position feedback · pose alignment</text>
<path d="M42 675 H1398" stroke="#234451"/><text x="42" y="711" class="foot">R51 · Relative face size is not sound distance · Metric depth is not yet enabled</text>
</svg>'''
(OUT/'system-architecture.svg').write_text(svg,encoding='utf-8')

def dot(cx,cy,label,sub,color,align):
    x=cx+36 if align=='start' else cx-36
    return f'<circle cx="{cx}" cy="{cy}" r="14" fill="{color}" stroke="#fff" stroke-width="4"/><text x="{x}" y="{cy-1}" text-anchor="{align}" class="sensor">{label}</text><text x="{x}" y="{cy+22}" text-anchor="{align}" class="small">{sub}</text>'

body=''.join([dot(335,160,'CH3','RIGHT EAR · MOVING','#06a89a','end'),
 dot(865,160,'CH2','LEFT EAR · MOVING','#06a89a','start'),
 dot(445,482,'CH4','RIGHT BODY · FIXED','#4985ce','end'),
 dot(755,482,'CH1','LEFT BODY · FIXED','#4985ce','start')])
svg2=f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="650" viewBox="0 0 1200 650" role="img" aria-labelledby="title desc">
<title id="title">Four microphone placement</title><desc id="desc">Front view. Bear left and right are opposite to the observer; two microphones move with the head and two remain on the body.</desc>
<style>text{{font-family:Inter,Arial,sans-serif}} .title{{font-size:28px;font-weight:700;fill:#102b3b}} .sub{{font-size:15px;fill:#54717e}} .sensor{{font-size:20px;font-weight:700;fill:#102b3b}} .small{{font-size:12px;fill:#54717e}} .label{{font-size:15px;fill:#406675;font-weight:600}}</style>
<rect width="1200" height="650" rx="24" fill="#f2f7f8"/><text x="42" y="57" class="title">FOUR MICROPHONES · TWO COORDINATE FRAMES</text>
<text x="42" y="86" class="sub">Conceptual front view · Left and right are named from the bear’s perspective · Not to scale</text>
<circle cx="385" cy="160" r="67" fill="#e3eded" stroke="#c5d8d8" stroke-width="3"/><circle cx="815" cy="160" r="67" fill="#e3eded" stroke="#c5d8d8" stroke-width="3"/>
<rect x="365" y="150" width="470" height="205" rx="100" fill="#fff" stroke="#c5d8d8" stroke-width="3"/>
<ellipse cx="505" cy="242" rx="39" ry="46" fill="#c9dddf"/><ellipse cx="695" cy="242" rx="39" ry="46" fill="#c9dddf"/>
<circle cx="505" cy="242" r="11" fill="#254c5b"/><circle cx="695" cy="242" r="11" fill="#254c5b"/>
<ellipse cx="600" cy="294" rx="18" ry="11" fill="#254c5b"/>
<rect x="430" y="374" width="340" height="205" rx="100" fill="#fff" stroke="#c5d8d8" stroke-width="3"/>
<line x1="335" y1="160" x2="865" y2="160" stroke="#06a89a" stroke-width="2" stroke-dasharray="6 6"/>
<line x1="445" y1="482" x2="755" y2="482" stroke="#4985ce" stroke-width="2" stroke-dasharray="6 6"/>
{body}<text x="600" y="133" text-anchor="middle" class="label">ears ≈ 13.55 cm</text>
<text x="600" y="518" text-anchor="middle" class="label">body pair ≈ 7.55 cm</text>
<text x="600" y="621" text-anchor="middle" class="sub">Moving head and fixed body require pose-aware coordinate alignment.</text></svg>'''
(OUT/'microphone-layout.svg').write_text(svg2,encoding='utf-8')
print('Generated system-architecture.svg and microphone-layout.svg')
