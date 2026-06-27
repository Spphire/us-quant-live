"""
Generate K-line (candlestick) style icon for the tray launcher.
Creates multi-resolution ICO file with dark theme.
"""
from PIL import Image, ImageDraw
from pathlib import Path


def draw_candlestick(draw, x, y_high, y_low, y_open, y_close, width, is_green):
    """Draw a single candlestick."""
    color_body = (16, 185, 129) if is_green else (239, 68, 68)  # green or red
    color_wick = (148, 163, 184)  # gray wick

    # Wick (vertical line)
    cx = x + width // 2
    draw.line([(cx, y_high), (cx, y_low)], fill=color_wick, width=max(1, width // 8))

    # Body (rectangle)
    body_top = min(y_open, y_close)
    body_bottom = max(y_open, y_close)
    if body_bottom - body_top < 2:
        body_bottom = body_top + 2
    draw.rectangle([(x, body_top), (x + width, body_bottom)], fill=color_body)


def create_kline_icon(size: int) -> Image.Image:
    """Create a K-line chart icon at the given size."""
    # Dark background
    img = Image.new('RGBA', (size, size), (10, 14, 39, 255))
    draw = ImageDraw.Draw(img)

    # Draw subtle border
    border = max(1, size // 32)
    draw.rectangle([(0, 0), (size - 1, size - 1)], outline=(30, 42, 74), width=border)

    # Calculate layout
    padding = max(2, size // 12)
    chart_top = padding
    chart_bottom = size - padding
    chart_height = chart_bottom - chart_top

    # 5 candlesticks: green, red, green, green, red (suggesting trend)
    num_candles = 5
    chart_width = size - 2 * padding
    candle_spacing = chart_width // num_candles
    candle_width = max(2, int(candle_spacing * 0.6))

    # Pattern: bullish trend
    # (high_pct, low_pct, open_pct, close_pct, is_green) - all as % of chart_height from top
    patterns = [
        (0.50, 0.95, 0.85, 0.60, True),   # green - bottom
        (0.40, 0.75, 0.55, 0.70, False),  # red - small pullback
        (0.30, 0.65, 0.65, 0.40, True),   # green - up
        (0.15, 0.45, 0.45, 0.25, True),   # green - up
        (0.10, 0.35, 0.20, 0.30, False),  # red - small consolidation
    ]

    for i, (h, l, o, c, g) in enumerate(patterns):
        x = padding + i * candle_spacing + (candle_spacing - candle_width) // 2
        y_high = chart_top + int(chart_height * h)
        y_low = chart_top + int(chart_height * l)
        y_open = chart_top + int(chart_height * o)
        y_close = chart_top + int(chart_height * c)
        draw_candlestick(draw, x, y_high, y_low, y_open, y_close, candle_width, g)

    return img


def main():
    out_dir = Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Standard ICO sizes for Windows
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [create_kline_icon(s) for s in sizes]

    # Save ICO (multi-resolution)
    ico_path = out_dir / "tray_icon.ico"
    images[0].save(
        ico_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"[OK] Saved ICO: {ico_path}")

    # Also save large PNG preview
    png_path = out_dir / "tray_icon_preview.png"
    images[-1].save(png_path)
    print(f"[OK] Saved PNG preview: {png_path}")


if __name__ == '__main__':
    main()
