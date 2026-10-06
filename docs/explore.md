# Plex TVX

Plex TVX is the store's read-only browser page: `plexdb explore`, served by `admin dev explore`
on `http://localhost:5194`. Its server is `plexdb/explore.py` and its one page is
`plexdb/explore.html`; [`file-map.md`](./file-map.md) describes both.

## Demo and clip modes

Two URL modes play a scripted tour of the movie title map instead of the interactive page. Both
live in `startDemo()` in `plexdb/explore.html`.

| URL | What it does |
|---|---|
| `/?demo` | Plays the tour live, looping for as long as the page is open. |
| `/?clip` | Draws the first frame, stays paused, and sets `window.__clip` for a recorder to step through frame by frame. |

Either mode forces the movie kind and the Title map view over whatever the URL's hash says, hides
the map's controls and the side panel, and ignores the pointer. The header and the View switch stay
on screen.

### The tour

The camera starts on the whole map, then lights three tags one after another — `western film`,
`superhero`, `hong kong` — gliding to the centre of each tag's titles and holding there, then
pulls back out to the whole map. A caption in the bottom-left corner names the lit tag and how
many of its movies are on the map.

The period, in seconds, is

```
DEMO_LEAD + (tags lit × DEMO_PER_TAG) + DEMO_OUT  =  0.4 + n × 1.0 + 1.1
```

A tag with no titles on this map is dropped before the tour starts, so `n` — and with it the
period — depends on the library. With all three tags present the period is 4.5 s. Read
`window.__clip.period` rather than assuming it.

### The recorder contract

```js
window.__clip = { period, seek(t) }
```

- **`period`** — the loop length in seconds.
- **`seek(t)`** — draws the frame at `t` seconds and returns a promise that resolves once that
  frame is painted (two animation frames after the draw). It is deterministic: the same `t`
  always draws the same frame, with no dependence on wall-clock time or on earlier seeks.
- **The loop seam** — `t` and `t + period` draw the same frame, so frames `0 … period` played
  in a loop show no jump. Any `t`, negative included, wraps into `[0, period)`.
- **Resizing** — a recorder may resize the window and call `seek` straight away, without
  waiting for the page's resize observer: `seek` re-measures the canvas first, and the
  whole-map frames refit to the new size.

`window.__clip` appears only once the three tags' title lists have been fetched and the map has
loaded. The page fetches the title lists first, with no time limit of its own, then gives the
map 120 s; a cold server takes tens of seconds. If the tour cannot start, `window.__clip` never
appears; instead `document.body.dataset.demo` is `"failed"` and the page's error banner reads
`Demo: <reason>` — a failed title-list fetch, the map not loading within its 120 s, or none of
the three tags being on the map. A recorder's own timeout should run longer than the page's.

A recorder loop, with Playwright:

```js
await page.goto("http://localhost:5194/?clip");
await page.waitForFunction(() => window.__clip || document.body.dataset.demo === "failed",
  null, { timeout: 180000 });
if (!(await page.evaluate(() => window.__clip !== undefined)))
  throw new Error(await page.textContent("#error"));
const period = await page.evaluate(() => window.__clip.period);
const fps = 30;
for (let i = 0; i < Math.round(period * fps); i++) {
  await page.evaluate(t => window.__clip.seek(t), i / fps);
  await page.screenshot({ path: `frame-${String(i).padStart(4, "0")}.png` });
}
```
