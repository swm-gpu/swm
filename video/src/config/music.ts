// Active music track — change this single constant to A/B test across all
// three compositions (Hero, Vertical, Square). Studio hot-reloads on save.
//
// Candidates:
//   '01-vintage'         · 2:53 · classic lofi, warm tape vibe (default)
//   '02-theta-frequency' · 2:06 · chill ambient, atmospheric
//   '03-two-hour-delay'  · 2:11 · winter lofi, slower tempo
//
// All three are CC0 1.0 Universal (public domain) by HoliznaCC0 from
// Free Music Archive. No attribution required. Files live in
// video/public/music/ and are not committed (re-fetch via the URLs in
// download-music.sh if you wipe the folder).
export const MUSIC_TRACK = '01-vintage';

// Background music gain. 0.35 sits comfortably under typing/UI sounds
// without overpowering the captions. Bump to 0.5 for a more present mix,
// drop to 0.2 for almost-silent ambient.
export const MUSIC_VOLUME = 0.35;
