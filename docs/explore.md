# Plex DB-EX

Plex DB-EX is the store's read-only browser page: `plexdb explore`, served by `admin dev explore`
on `http://localhost:5194`. Its server is `plexdb/explore.py` and its one page is
`plexdb/explore.html`; [`file-map.md`](./file-map.md) describes both.

## Related tags

Selecting a tag opens its panel with two tabs, **Related tags** and **Titles**, each with its count.
The tab last picked stays picked for the next tag. Before any pick, the Tag network opens on Related
tags and the other views open on Titles.

Related tags lists every tag that shares a title with the selected one, from
`/api/cotags?kind=&tag=`. The server counts that over the folded keyword rows the tag table was built
from (`_IndexCache.cotags`), so the counts agree with the table's df and a request never reads the
store. On the 2026-10 movie store a request takes 13–300 ms; the slowest is science fiction, with
5,904 co-tags.

Each row the server sends is `[tag, shared, df]`, unfiltered and unranked; the page does the rest,
so moving the slider never asks the server again.

**Lift** is `shared × N / (df_anchor × df_tag)`: how many times likelier the tag is on the selected
tag's titles than on any title of the kind. A lift resting on two titles is mostly luck, so the
table shows a **cautious lift** instead (`cautious_lift` in `explore.py`, `cautiousLift` in the
page, held to the same numbers by a test). It takes the selected tag's share at the Wilson lower
bound with `z` standard errors of evidence.

- **The Evidence slider, from weak to strong,** sets `z`, from 0 to 4. At weak, the cautious lift is
  the plain lift. Toward strong, a lift from few titles shrinks far faster than one from many:
  heist's 2-title pairs fall from 62.7× to 16.8× at the middle, while jewelry heist (8 titles)
  keeps 28.1×.
- **A tag is listed when its cautious lift is above 1×.** Below that, the pairing could be chance.
  Heist lists 400 tags at weak, 134 at the middle and 61 at strong, at the network's default minimum
  of 25 titles per tag.
- **The table sorts by any column.** Its columns are Tag, Lift (the cautious lift) and Shared.
  Clicking a header sorts by it, and clicking it again reverses the order. Ties go to more shared
  titles.
- **The slider and the sort stay where you left them,** from tag to tag and across reloads
  (`plexdb-explore:co-strength`, `plexdb-explore:co-sort` in localStorage).

The tag table's "Related tags" column shows each tag's five highest cautious lifts at the slider's
middle (`CO_STRENGTH`, z = 2), as `related` in `/api/tags`.

In the Tag network, the list holds only the tags drawn on the graph. A tag the "Minimum titles per
tag" slider hides is gone from both the graph and the list, and lowering that slider brings it back
in both. Every listed tag is lit, so the list and the lit set are always the same tags. The graph
draws four levels:

| Level | Drawn as |
|---|---|
| The selected tag and the tag under the pointer (in the list or on the graph) | centre colour with an ink ring and a bold label; the hovered tag gets an orange spoke |
| Listed tags | full accent and ringed, labelled where there is room, each joined to the selected tag by a spoke |
| Tags on a title with it whose pairing could be chance at this evidence | half-strength accent, no label |
| Tags never on a title with it | small and grey |

The network's stored strongest-six edges stay as a faint texture. The crosshair button beside the
slider zooms to the selected tag and its listed tags. The note in the corner counts each level among
the drawn nodes.

A selection always has a way out. Any of these calls `clearTag`, which empties the panel, the
graph's levels and the `tag` in the URL:

- a click on empty canvas (a drag that pans does not count),
- Esc, unless focus is in a text field,
- the panel's × button,
- a second click on the selected table row,
- switching between Movies and Shows.

On the title map, a click on empty canvas unpins the pinned card, as Esc already did.

## Search

The Search tab finds Shots in the Frame index by what they look like. It is look-only: it saves,
accepts and labels nothing. It needs `PLEXDB_FRAMES_DIR`, an encoder (`PLEXDB_FRAME_ENCODER_URL`,
`PLEXDB_FRAME_MODEL`), the `plexdb-read` binary (`PLEXDB_READ_BIN`), ffmpeg and ffprobe, `PLEX_URL`
with `PLEX_TOKEN`, and the library read-only at `PLEXDB_MEDIA_DIR` with `PLEX_SOURCE_ROOTS`;
`/api/search/status` reports each one found or missing with its reason, and the page shows a banner
for every one that is missing.

- **Pool.** The side panel holds the titles a search covers; an empty pool searches every title
  `frame_index_state` holds with a `model`, so a search needs no titles picked, and the empty panel
  reads "All indexed titles (N)". The add box offers titles and
  collections (a collection adds its members). Each title shows `indexed`, `not indexed`, `queued` or
  `failed`. A search whose pool has unindexed titles shows "Only N of M titles in this pool are
  indexed" and a "Queue the other M−N" link, which writes `frame_queue` rows through
  `frames.queue`, as `plexdb frames queue` does (`queued_by` is `explore`). The pool lives in the
  browser's localStorage.
- **Frame index panel.** Under the Pool, `GET /api/search/frames` shows the title the harvester is
  indexing now and since when, each Plex section's indexed and total titles, the queued count and the
  harvest hours (`frames.status`, the report `plexdb frames status` prints). Each Plex section's
  title set is read once and kept until the store file changes, so only the first request after a
  sweep rescans `plex_items` (about 1.5 s on the 87,000-title store; a refresh after that takes
  about 0.2 s). Below it, the 20 newest titles whose last attempt failed, with a line counting the
  rest: its name and the failure's reason, which opens to the item_id and when it failed. Retry
  queues that title through the same `POST /api/search/queue`; a failed title already in the queue
  shows `queued` instead. The endpoint returns each title's `reason`, a short phrase with no path in
  it, and never its `error`, since the page has no login; a title that failed before `reason`
  existed reads "failed (see harvester log)". The full error stays in `frames.db`, in
  `plexdb frames status` and in the harvester log.
- **Token boxes.** About 150 ms after typing stops, the page posts the text to
  `/api/search/tokenize`. The server tokenizes it with the Hugging Face tokenizer for
  `PLEXDB_FRAME_MODEL` and returns each token with its character offsets; the page draws one box per
  token, so a word the tokenizer splits is two adjacent boxes. The counter reads `N / 75`. Past 75 it
  turns red, the tokens past the limit are struck through, and the search cuts the text after the 75th
  token before the encoder sees it, so the "not searched" line is true.
- **Tokenizer file.** `PLEXDB_FRAME_TOKENIZER` names a `tokenizer.json`. Unset, the server reads
  `tokenizer-<model>.json` from the explorer's data directory (`/explore-data` in the container),
  with `/` in the model name written as `--`. It never fetches one: the tab makes no outbound call
  besides Plex and the encoder. With neither file present, `/api/search/status` lists
  `problems.tokenizer`, which names the setting and the path to fill. Download the model's `tokenizer.json` from its Hugging Face
  page once and place it there; a container already holding that file keeps working offline.
- **Advanced.** Closed by default. Open, it holds one negative field (each comma-separated part is its
  own negative, with its own boxes and count), a negative-weight slider, a temperature slider (0 is
  off) and a draw control (count, seed, Draw). Closed while any is set, its summary names them, for
  example `3 negatives, weight 0.5 · temperature 0.010 · draw 1, seed 42`.
- **Results.** `POST /api/search/run` runs `plexdb-read search` (`plexdb/shot_search.py` builds its
  command line, the same code `plexdb search` uses) and returns the top 100 Shots by score, with each
  title, timecodes, three frames, phrase similarity, the strongest negative and its similarity, and
  with a temperature each Shot's odds. Draw runs the reader's draw a second time with the same
  inputs and marks the drawn rows, so the picks equal `plexdb search … --draw N --seed S`.
- **Frames and Play.** The server asks Plex which file the indexed part is (a metadata read, which
  starts no playback session), then cuts a frame from that file under `PLEXDB_MEDIA_DIR` with ffmpeg
  (`/api/search/frame`), and a Shot's in–out window as a 640 px wide H.264 MP4 without audio
  (`/api/search/clip`, with range requests). Both are cached under `<explore data>/clips/`, which
  holds at most 2 GB: past it the least recently used file goes first. The browser asks only the
  explorer's own server; it never reaches Plex, the encoder or a tokenizer (ADR-0017).
- **Log.** Each search, queue, frame and clip writes one `explore:` line on stderr with the raw
  values, and `admin logs live` shows them in the container.

`POST /api/search/queue` is the tab's one write outside its own `clips/` cache, and it asks for no
login: anyone who reaches the explorer's port can call it. The container publishes that port on one
host address only, `PLEXDB_EXPLORE_BIND` (`[[docker_run.ports]]` in `admin.toml`), and publishes
nothing while that or `PLEXDB_EXPLORE_PORT` is unset. The route (`queue_missing` in
`plexdb/explore_search.py`) adds `frame_queue` rows to `frames.db` with `queued_by` `explore`, only
for titles the explorer's store (the snapshot, in the container) lists in `items` and the Frame
index holds as not indexed or `failed`; an id outside `items` is skipped, a title already queued
keeps its place, and a call naming more than 5,000 ids is refused whole.
So a caller can move library titles ahead of the harvest's backlog and retry failed ones, but
cannot queue work outside the library. `/frames` is mounted read-write in the container because the
harvester running beside the explorer writes through it; giving the explorer a read-only mount of
its own waits on splitting it into its own container (plex-db-ex-bsh).

## Demo and clip modes

Two URL modes play a scripted tour of the movie title map instead of the interactive page. Both
live in `startDemo()` in `plexdb/explore.html`.

| URL | What it does |
|---|---|
| `/?demo` | Plays the tour live, looping for as long as the page is open. |
| `/?clip` | Draws the first frame, stays paused, and sets `window.__clip` for a recorder to step through frame by frame. |

Either mode forces the Tags tab, the movie kind and the Title map view over whatever the URL's hash
says, hides
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

The `test_clip_*` tests in `tests/test_explore.py` hold the page to the seam and the resize rule
in a headless Chromium, as part of `admin vet`, and the `test_demo_*` tests hold `?demo` to a
camera that moves on its own with no `window.__clip`. `admin build` fetches that browser.

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
