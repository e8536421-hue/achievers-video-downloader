from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

canvas = Image.new('RGB', (1200, 630), '#0b1715')
draw = ImageDraw.Draw(canvas)
def font(size, bold=False):
    return ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf', size)
draw.rounded_rectangle((850, -170, 1360, 760), radius=250, fill='#14382b')
draw.ellipse((950, 40, 1340, 430), fill='#20523e')
draw.ellipse((72, 52, 126, 106), fill='#85d5b2')
draw.text((86, 58), 'A', font=font(34, True), fill='#0b1715')
draw.text((144, 65), 'ACHIEVERS', font=font(25, True), fill='#ffffff')
draw.text((72, 159), 'Found a video', font=font(76, True), fill='#ffffff')
draw.text((72, 244), 'worth keeping?', font=font(76, True), fill='#85d5b2')
draw.text((76, 355), 'Paste a link. Choose a format. Save it.', font=font(30), fill='#d4e5dc')
draw.rounded_rectangle((76, 428, 400, 490), radius=31, fill='#85d5b2')
draw.text((104, 443), 'Try Achievers free', font=font(28, True), fill='#0b1715')
draw.text((76, 545), 'Supported public videos  /  No account required', font=font(23), fill='#a9bfb3')
draw.rounded_rectangle((930, 240, 1110, 420), radius=40, fill='#85d5b2')
draw.polygon([(991, 280), (991, 375), (1061, 328)], fill='#15392b')
canvas.save(Path(__file__).parent / 'static' / 'share-preview-v1.png', optimize=True)
