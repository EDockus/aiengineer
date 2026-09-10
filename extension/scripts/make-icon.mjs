/* Generates images/logo.png (128x128) with no image dependencies. */
import { deflateSync } from "node:zlib";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const S = 128;
const BG = [0x00, 0x78, 0xd4];
const FG = [0xff, 0xff, 0xff];

const px = Buffer.alloc(S * S * 3);
const set = (x, y, c) => {
  if (x < 0 || y < 0 || x >= S || y >= S) return;
  const i = (y * S + x) * 3;
  px[i] = c[0]; px[i + 1] = c[1]; px[i + 2] = c[2];
};
for (let y = 0; y < S; y++) for (let x = 0; x < S; x++) set(x, y, BG);

const disc = (cx, cy, r, c) => {
  for (let y = cy - r - 1; y <= cy + r + 1; y++)
    for (let x = cx - r - 1; x <= cx + r + 1; x++)
      if ((x - cx) ** 2 + (y - cy) ** 2 <= r * r) set(x, y, c);
};
const ring = (cx, cy, r, w, c) => {
  for (let y = cy - r - 1; y <= cy + r + 1; y++)
    for (let x = cx - r - 1; x <= cx + r + 1; x++) {
      const d2 = (x - cx) ** 2 + (y - cy) ** 2;
      if (d2 <= r * r && d2 >= (r - w) ** 2) set(x, y, c);
    }
};
const vline = (x, y0, y1, w, c) => {
  for (let y = y0; y <= y1; y++) for (let x2 = x - w; x2 <= x + w; x2++) set(x2, y, c);
};
const hline = (y, x0, x1, w, c) => {
  for (let x = x0; x <= x1; x++) for (let y2 = y - w; y2 <= y + w; y2++) set(x, y2, c);
};

// git-branch glyph: trunk with two nodes, branching to a third node.
vline(42, 34, 94, 3, FG);
hline(64, 42, 82, 3, FG);
disc(42, 30, 11, FG); ring(42, 30, 11, 11, FG);
disc(42, 98, 11, FG);
disc(86, 64, 11, FG);
// punch holes so the nodes read as rings, like the ADO branch icon
disc(42, 30, 5, BG);
disc(42, 98, 5, BG);
disc(86, 64, 5, BG);

// PNG assembly
const raw = Buffer.alloc(S * (S * 3 + 1));
for (let y = 0; y < S; y++) {
  raw[y * (S * 3 + 1)] = 0; // filter: none
  px.copy(raw, y * (S * 3 + 1) + 1, y * S * 3, (y + 1) * S * 3);
}
const crcTable = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
const crc32 = (buf) => {
  let c = 0xffffffff;
  for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
};
const chunk = (type, data) => {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
};
const ihdr = Buffer.alloc(13);
ihdr.writeUInt32BE(S, 0); ihdr.writeUInt32BE(S, 4);
ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
const png = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  chunk("IHDR", ihdr),
  chunk("IDAT", deflateSync(raw, { level: 9 })),
  chunk("IEND", Buffer.alloc(0)),
]);
const out = resolve(dirname(fileURLToPath(import.meta.url)), "../images/logo.png");
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, png);
console.log(`wrote ${out} (${png.length} bytes)`);
