"""Create a dimensioned ChArUco print target and verify its marker layout."""
import argparse
import json
from pathlib import Path


SPEC = {"type":"charuco", "squares_x":7, "squares_y":10,
        "square_length_m":.020, "marker_length_m":.015,
        "dictionary":"DICT_4X4_100", "legacy_pattern":False}


def pattern(folder):
    import cv2
    import numpy as np
    board = cv2.aruco.CharucoBoard((7,10),.020,.015,
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_100))
    image = board.generateImage((1400,2000),marginSize=0,borderBits=1)
    # 10 pixels/mm; white margin is 2 mm outside the actual board boundary.
    image = cv2.copyMakeBorder(image,20,20,20,20,cv2.BORDER_CONSTANT,value=255)
    if not cv2.imwrite(str(folder/"charuco-7x10-20mm.png"),image):
        raise RuntimeError("Could not write board image")
    corners, ids, _, marker_ids = cv2.aruco.CharucoDetector(board).detectBoard(image)
    count = 0 if ids is None else len(ids)
    if count != 54:
        raise RuntimeError(f"Generated board has only {count}/54 detected corners")
    spec = {**SPEC,"opencv_generated":cv2.__version__,"board_width_mm":140,"board_height_mm":200,
            "image_width_mm":144,"image_height_mm":204,"image_pixels":[1440,2040],
            "verification":{"charuco_corners":count,"markers":len(marker_ids)},
            "printing":"A4 portrait, actual size / 100%, no fit-to-page; verify the 100 mm ruler"}
    (folder/"board-spec.json").write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(spec))


def pdf(folder):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from pypdf import PdfReader
    pdfmetrics.registerFont(TTFont("BoardChinese","C:/Windows/Fonts/msyh.ttc",subfontIndex=0))
    path=folder/"stereo-charuco-A4-20mm.pdf"
    c=canvas.Canvas(str(path),pagesize=A4)
    c.setTitle("DeepSight-3D - A4 stereo ChArUco board - 20 mm squares")
    w,h=A4
    c.setFont("BoardChinese",17)
    c.drawCentredString(w/2,281*mm,"双目摄像头标定板")
    c.setFont("BoardChinese",10)
    c.drawCentredString(w/2,271*mm,"ChArUco 7 × 10 格；每格边长 20 mm")
    c.setFont("Helvetica",9)
    c.drawCentredString(w/2,264*mm,"DICT_4X4_100  |  marker: 15 mm  |  board: 140 x 200 mm")
    c.drawImage(str(folder/"charuco-7x10-20mm.png"),(w-144*mm)/2,(h-204*mm)/2,
                width=144*mm,height=204*mm,mask=None)
    # This ruler is outside the calibration pattern and measures exactly 100 mm.
    x=(w-100*mm)/2; y=34*mm
    c.setLineWidth(.5)
    c.line(x,y,x+100*mm,y)
    for i in range(11):
        c.line(x+i*10*mm,y-2*mm,x+i*10*mm,y+2*mm)
    c.setFont("BoardChinese",8.5)
    c.drawCentredString(w/2,26*mm,"打印后核对：上方两端之间应为 100 mm；每格应为 20 mm。")
    c.drawCentredString(w/2,19*mm,"A4 纵向，实际大小 / 100%；关闭“适应页面”，不要裁掉白边。")
    c.drawCentredString(w/2,12*mm,"整张平贴硬板，不起皱、不覆亮膜；手持在两镜头前移动，不贴在人头上。")
    c.showPage(); c.save()
    page=PdfReader(str(path)).pages[0]
    if abs(float(page.mediabox.width)-w)>.01 or abs(float(page.mediabox.height)-h)>.01:
        raise RuntimeError("PDF page is not A4")
    print(json.dumps({"pdf":str(path.resolve()),"pages":1,"paper_mm":[210,297],
                      "board_mm":[140,200],"square_mm":20,"ruler_mm":100}))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--pdf",action="store_true")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    pdf(args.output) if args.pdf else pattern(args.output)
