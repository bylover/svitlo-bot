"""🖼 Фото й відео під Instagram: точний розмір (4:5, 9:16, 1:1, 3:4), обрізка, sRGB, оптимальне стиснення; HEIC → JPG; відео — H.264/AAC.
Запуск із бота (media.yml): CHAT, FILE_ID, AR, CROP, KIND; секрет BOT_TOKEN."""
import io
import json
import os
import subprocess
import urllib.parse
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
CHAT, FILE_ID = os.environ["CHAT"], os.environ["FILE_ID"]
AR, CROP, KIND = os.environ.get("AR", "4:5"), os.environ.get("CROP", "center"), os.environ.get("KIND", "photo")
SIZES = {"4:5": (1080, 1350), "9:16": (1080, 1920), "1:1": (1080, 1080), "3:4": (1080, 1440)}
API = f"https://api.telegram.org/bot{TOKEN}"


def tg(method: str, **data):
    req = urllib.request.Request(f"{API}/{method}", data=urllib.parse.urlencode(data).encode())
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def send_doc(path: str, name: str, caption: str):
    import uuid
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in (("chat_id", CHAT), ("caption", caption), ("parse_mode", "HTML")):
        body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    body.write(open(path, "rb").read()); body.write(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(f"{API}/sendDocument", data=body.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    urllib.request.urlopen(req, timeout=120).read()


def fail(msg: str):
    tg("sendMessage", chat_id=CHAT, text=f"😕 Не вдалося обробити файл: {msg}")
    raise SystemExit(0)


def main():
    info = tg("getFile", file_id=FILE_ID).get("result") or {}
    if not info.get("file_path"):
        fail("Telegram не віддав файл (можливо, більший за 20 МБ)")
    src = "in_" + os.path.basename(info["file_path"])
    urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", src)
    w, h = SIZES.get(AR, SIZES["4:5"])
    size_in = os.path.getsize(src)
    if KIND == "video":
        # обрізка до пропорції (по центру / зверху / знизу), масштаб, H.264 + AAC, 30 к/с, ~10 Мбіт/с, faststart
        y = {"top": "0", "bottom": "(ih-oh)", "center": "(ih-oh)/2"}[CROP if CROP in ("top", "bottom") else "center"]
        vf = f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})':(iw-ow)/2:{y},scale={w}:{h}:flags=lanczos,setsar=1"
        out = "out.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-r", "30", "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                            "-b:v", "10M", "-maxrate", "12M", "-bufsize", "20M", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", out],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            fail("ffmpeg: " + r.stderr[-200:])
        send_doc(out, f"video_{w}x{h}.mp4", f"🎬 Готово: {w}×{h} ({AR}), H.264, 30 к/с, ~10 Мбіт/с, звук AAC 192 кбіт/с\n"
                                              f"Було {size_in / 1e6:.1f} МБ → стало {os.path.getsize(out) / 1e6:.1f} МБ")
        return
    from PIL import Image, ImageCms, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        im = Image.open(src)
    except Exception as e:
        fail(f"не відкрився як зображення ({e})")
    im = ImageOps.exif_transpose(im)
    w0, h0, fmt0 = im.width, im.height, (im.format or "?")
    icc = im.info.get("icc_profile")
    if icc:                                           # колірний профіль → sRGB (так кольори в Instagram не «пливуть»)
        try:
            im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:
            pass
    im = im.convert("RGB")
    target = w / h
    if im.width / im.height > target:                 # ширше → обрізаємо боки
        nw = round(im.height * target); x = (im.width - nw) // 2
        im = im.crop((x, 0, x + nw, im.height))
    else:                                             # вище → обрізаємо зверху/знизу за вибором
        nh = round(im.width / target)
        y = 0 if CROP == "top" else im.height - nh if CROP == "bottom" else (im.height - nh) // 2
        im = im.crop((0, y, im.width, y + nh))
    im = im.resize((w, h), Image.LANCZOS)
    out = "out.jpg"
    im.save(out, "JPEG", quality=88, optimize=True, progressive=True, subsampling=0)
    crop_ua = {"top": "зверху", "bottom": "знизу"}.get(CROP, "по центру")
    send_doc(out, f"photo_{w}x{h}.jpg", f"🖼 Готово: {w}×{h} ({AR}), JPEG sRGB, якість 88%, обрізка {crop_ua}\n"
                                          f"Було: {w0}×{h0}, {fmt0}, {size_in / 1e6:.1f} МБ → стало {os.path.getsize(out) / 1e3:.0f} КБ\n"
                                          "<i>Завантажуйте файлом з телефона й увімкніть в Instagram «Завантажувати в найвищій якості».</i>")


if __name__ == "__main__":
    main()
