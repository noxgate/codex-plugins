#!/usr/bin/env python3
"""Split frozen sheet regions, remove a chroma background, and validate assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from asset_batch import AssetError, atomic_json, write_image


def rect_pixels(rect, size):
    if not isinstance(rect, list) or len(rect) != 4 or not all(type(v) in (int, float) for v in rect):
        raise AssetError('rect must contain four normalized numbers')
    x0, y0, x1, y1 = rect
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise AssetError('rect is outside the sheet')
    return tuple(round(v*size[i % 2]) for i, v in enumerate(rect))


def load_manifest(path):
    data = json.loads(path.read_text())
    if data.get('version') != '1.0' or not data.get('sheets'):
        raise AssetError('sheet manifest needs version 1.0 and nonempty sheets')
    ids, sheet_ids = set(), set()
    for sheet in data['sheets']:
        if sheet['id'] in sheet_ids:
            raise AssetError('duplicate sheet ID')
        sheet_ids.add(sheet['id'])
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', sheet['key_color']):
            raise AssetError('invalid key color')
        if len(sheet['size']) != 2 or any(type(v) is not int or not 1 <= v <= 8192 for v in sheet['size']):
            raise AssetError('invalid sheet size')
        if not 0 <= sheet['background_tolerance'] < sheet['foreground_threshold'] <= 442:
            raise AssetError('invalid matting thresholds')
        if not sheet.get('items'):
            raise AssetError('sheet has no output items')
        rectangles = []
        for item in sheet['items']:
            asset_id = item['asset_id']
            if not re.fullmatch(r'[a-z][a-z0-9_]{1,79}', asset_id) or asset_id in ids:
                raise AssetError('invalid or duplicate asset ID')
            ids.add(asset_id)
            if len(item['target_size']) != 2 or any(type(v) is not int or not 16 <= v <= 4096 for v in item['target_size']):
                raise AssetError('invalid target size')
            if len(item['anchor']) != 2 or any(not 0 <= v <= 1 for v in item['anchor']):
                raise AssetError('invalid anchor')
            if type(item.get('max_components', 1)) is not int or not 1 <= item.get('max_components', 1) <= 8:
                raise AssetError('invalid component count')
            if not 0 <= sheet['margin_px'] < min(item['target_size'])/2:
                raise AssetError('invalid target margin')
            rectangles.append(rect_pixels(item['rect'], sheet['size']))
        rectangles += [rect_pixels(rect, sheet['size']) for rect in sheet.get('empty_rects', [])]
        for i, a in enumerate(rectangles):
            for b in rectangles[i+1:]:
                if max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3]):
                    raise AssetError('sheet regions overlap')
    return data['sheets']


def matte(image, key_color, tolerance, foreground):
    rgb = np.asarray(image.convert('RGB'), dtype=np.float32)
    expected = np.array([int(key_color[i:i+2], 16) for i in (1, 3, 5)], dtype=np.float32)
    border = np.concatenate((rgb[:3].reshape(-1, 3), rgb[-3:].reshape(-1, 3),
                             rgb[:, :3].reshape(-1, 3), rgb[:, -3:].reshape(-1, 3)))
    key = np.median(border, axis=0)
    if np.linalg.norm(key-expected) > 60 or np.percentile(np.linalg.norm(border-key, axis=1), 95) > tolerance:
        raise AssetError('background is not flat or foreground touches region boundary')
    distance = np.linalg.norm(rgb-key, axis=2)
    alpha = np.clip((distance-tolerance)/(foreground-tolerance), 0, 1)
    low = int(np.argmin(key))
    high = np.where(key > key[low]+100)[0]
    if not len(high):
        raise AssetError('key color must have a distinct chroma channel')
    key_excess = np.min(rgb[...,high], axis=2)-rgb[...,low]
    known = (distance >= foreground) & (key_excess <= tolerance)
    edge = (distance > tolerance) & ~known
    estimate = rgb.copy()
    # Propagate opaque foreground colors into the narrow antialias fringe to unmix the key color.
    for _ in range(8):
        if np.all(known[edge]):
            break
        total = np.zeros_like(rgb)
        count = np.zeros(alpha.shape, dtype=np.float32)
        for dy, dx in ((-1,0), (1,0), (0,-1), (0,1), (-1,-1), (-1,1), (1,-1), (1,1)):
            shifted = np.roll(known, (dy, dx), axis=(0,1)).copy()
            if dy < 0: shifted[dy:] = False
            if dy > 0: shifted[:dy] = False
            if dx < 0: shifted[:, dx:] = False
            if dx > 0: shifted[:, :dx] = False
            total += np.roll(estimate, (dy,dx), axis=(0,1))*shifted[..., None]
            count += shifted
        added = (~known) & (count > 0)
        estimate[added] = total[added]/count[added, None]
        known |= added
    vector = estimate-key
    projected = np.sum((rgb-key)*vector, axis=2)/np.maximum(np.sum(vector*vector, axis=2), 1)
    alpha[edge & known] = np.clip(projected[edge & known], 0, 1)
    unresolved = int(np.count_nonzero(edge & ~known))
    if unresolved > max(50, int(np.count_nonzero(distance > tolerance)*.01)):
        raise AssetError('broad translucent or keyed fringe requires dedicated matting; do not silently flatten it')
    color = np.clip((rgb-(1-alpha[..., None])*key)/np.maximum(alpha[..., None], .001), 0, 255)
    rgba = np.concatenate((color, alpha[..., None]*255), axis=2).astype(np.uint8)
    rgba[edge & ~known] = 0
    rgba[rgba[...,3] <= 16] = 0
    rgba[rgba[...,3] == 0, :3] = 0
    return Image.fromarray(rgba), {'measured_key': key.astype(int).tolist(), 'unresolved_edge_pixels': unresolved}


def major_components(mask):
    small = mask.copy()
    small.thumbnail((256,256), Image.Resampling.NEAREST)
    pixels = np.asarray(small) > 127
    seen = np.zeros(pixels.shape, dtype=bool)
    sizes = []
    height, width = pixels.shape
    for y, x in zip(*np.where(pixels)):
        if seen[y,x]:
            continue
        queue = deque([(y,x)])
        seen[y,x] = True
        count = 0
        while queue:
            yy, xx = queue.popleft()
            count += 1
            for dy, dx in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
                ny, nx = yy+dy, xx+dx
                if 0 <= ny < height and 0 <= nx < width and pixels[ny,nx] and not seen[ny,nx]:
                    seen[ny,nx] = True
                    queue.append((ny,nx))
        sizes.append(count)
    return sum(size >= max(8, max(sizes, default=0)*.05) for size in sizes)


def normalize(image, item, margin):
    mask = image.getchannel('A')
    bbox = mask.point(lambda value: 255 if value > 32 else 0).getbbox()
    if bbox is None:
        raise AssetError(f'{item["asset_id"]}: empty foreground')
    if min(bbox[0], bbox[1], image.width-bbox[2], image.height-bbox[3]) < 4:
        raise AssetError(f'{item["asset_id"]}: foreground is clipped at crop boundary')
    count = major_components(mask)
    if count > item.get('max_components', 1):
        raise AssetError(f'{item["asset_id"]}: unexpected multiple foreground objects ({count})')
    # Include the full alpha fringe when trimming, even where it is faint.
    full_bbox = mask.getbbox()
    trim = image.crop(full_bbox)
    width, height = item['target_size']
    trim.thumbnail((width-2*margin, height-2*margin), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', (width,height))
    canvas.alpha_composite(trim, ((width-trim.width)//2, (height-trim.height)//2))
    return canvas, {'source_bbox': list(full_bbox), 'major_components': count}


def png_bytes(image):
    import io
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def validate_image(item, image):
    if list(image.size) != item['target_size'] or image.mode != 'RGBA':
        raise AssetError(f'{item["asset_id"]}: output size or mode mismatch')
    alpha = image.getchannel('A')
    if alpha.getextrema() != (0,255):
        raise AssetError(f'{item["asset_id"]}: missing transparent/opaque coverage')


def validate_output(item, path):
    with Image.open(path) as image:
        validate_image(item, image)


def process(manifest, output_dir):
    sheets = load_manifest(manifest)
    pending, records = [], []
    for sheet in sheets:
        source = (manifest.parent/sheet['source']).resolve()
        with Image.open(source) as image:
            if list(image.size) != sheet['size']:
                raise AssetError('source sheet size differs from frozen regions; revise manifest explicitly')
            image = image.convert('RGB')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            for rect in sheet.get('empty_rects', []):
                empty, _ = matte(image.crop(rect_pixels(rect, image.size)), sheet['key_color'],
                                 sheet['background_tolerance'], sheet['foreground_threshold'])
                alpha = np.asarray(empty.getchannel('A'))
                if np.count_nonzero(alpha > 32) > 4:
                    raise AssetError('an explicitly empty sheet region contains extra content')
            for item in sheet['items']:
                crop = image.crop(rect_pixels(item['rect'], image.size))
                cutout, matte_info = matte(crop, sheet['key_color'], sheet['background_tolerance'], sheet['foreground_threshold'])
                result, crop_info = normalize(cutout, item, sheet['margin_px'])
                validate_image(item, result)
                pending.append((item, result))
                records.append({'asset_id': item['asset_id'], 'file_name': item['asset_id']+'.png',
                                'source_sheet': sheet['id'], 'source_sha256': digest, 'rect': item['rect'],
                                'size': item['target_size'], 'anchor': item['anchor'],
                                'status': 'pending_art_review', **matte_info, **crop_info})
    output_dir.mkdir(parents=True, exist_ok=True)
    for item, image in pending:
        write_image(output_dir/(item['asset_id']+'.png'), png_bytes(image))
        validate_output(item, output_dir/(item['asset_id']+'.png'))
    atomic_json(output_dir/'split-report.json', {'version': '1.0', 'assets': records})
    preview(pending, output_dir/'review.png')
    preview(pending, output_dir/'review-light.png', '#ffffff')
    preview(pending, output_dir/'review-dark.png', '#202724')
    return records


def preview(items, path, background=None):
    canvas = Image.new('RGB', (1000, ((len(items)+4)//5)*360), background or '#f1f4f2')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=14)
    for index, (item, image) in enumerate(items):
        x = (index%5)*200
        top = (index//5)*360
        if background is None:
            for y in range(0,300,16):
                for xx in range(x,x+200,16):
                    draw.rectangle((xx,top+y,xx+15,top+y+15), fill='#d5ddd7' if (xx//16+y//16)%2 else '#ffffff')
        display = image.copy()
        display.thumbnail((184,284), Image.Resampling.LANCZOS)
        canvas.paste(display, (x+(200-display.width)//2,top+(300-display.height)//2), display)
        draw.text((x+8,top+315), item['asset_id'], font=font, fill='#cbdacf' if background == '#202724' else '#253c36')
    write_image(path, png_bytes(canvas))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'split', 'validate'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    try:
        sheets = load_manifest(args.manifest)
        if args.command == 'plan':
            print(f'sheets={len(sheets)} assets={sum(len(sheet["items"]) for sheet in sheets)}')
        elif args.output_dir is None:
            parser.error('split/validate requires --output-dir')
        elif args.command == 'split':
            records = process(args.manifest, args.output_dir)
            print(f'split {len(records)} RGBA assets; pending art review')
        else:
            for sheet in sheets:
                for item in sheet['items']:
                    validate_output(item, args.output_dir/(item['asset_id']+'.png'))
            print('all output dimensions and alpha channels valid')
    except (AssetError, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'error: {error}\n')


if __name__ == '__main__':
    main()
