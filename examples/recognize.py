"""运行方式：uv run python examples/recognize.py frame.jpg。"""

import argparse
import asyncio
import time
from pathlib import Path

from hos_frame_clock import _crop, recognize_frame


async def main():
    parser = argparse.ArgumentParser(description="识别视频帧右上角时间")
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    image_bytes = args.image.read_bytes()
    output_dir = Path(__file__).resolve().parent / "tmp"
    output_dir.mkdir(exist_ok=True)
    crop_path = output_dir / f"{args.image.stem}_crop.png"
    crop_path.write_bytes(_crop(image_bytes))
    print(f"裁剪图片：{crop_path}")
    started = time.monotonic()
    result = await recognize_frame(image_bytes)
    print(f"elapsed_seconds={time.monotonic() - started:.3f}")
    if result is None:
        print("未识别到唯一有效时间")
    else:
        print(result.timestamp.strftime("%Y-%m-%d %H:%M:%S"))
        print(f"raw_text={result.raw_text!r}")


if __name__ == "__main__":
    asyncio.run(main())
