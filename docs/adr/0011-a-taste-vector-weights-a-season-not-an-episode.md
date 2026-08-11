# A taste vector weighs one season, damped, and carries no ranking knobs

The Layer 2 rollup weighs each title a user watched by `sqrt(r)`, where `r` is that user's
consumption of the title measured in **seasons** — a film, or one full season of a show, is
`r = 1`. Titles below `r = 0.5` contribute nothing. There is no recency decay, no negative
weight for an abandoned watch, and no tunable constant anywhere in the rollup.

## Why

Issue [#13](https://github.com/McBrideMusings/plex-db-ex/issues/13) asked who owns three ranking
knobs — recency half-life, exploration fraction, negative-signal weight — and what their values
are. It refused to answer on intuition and set its own precondition: *"build the rollup, look at
real vectors for real users, then decide."* That was done against 25,835 real plays across 17
accounts. Two of the three knobs turned out not to exist, and the third turned out not to belong
here.

**The unit is a season, because every other unit encodes something that is not taste.**

- *Once per episode* measures how much of your life a title occupied. One 313-episode sitcom took
  six of the top eighteen keywords in a real vector — `restaurant`, `menu`, `new jersey`,
  `seaside town`, `puberty`, `singing` — on volume alone.
- *Once per title* says a nine-minute stand-up special and a seven-season run are the same thing.
- *Fraction of the episodes the library holds* — the shape Hu, Koren and Volinsky use, see below —
  makes the denominator a property of what happens to sit on disk. At 313 episodes held, a "full
  watch" needs 157 before the title counts at all, so a show long enough that nobody finishes it
  is structurally deleted. On real data this discarded 98 watched episodes of one show as
  "not a strong indication that the user likes the program".
- *A season* is the unit a show is written, released and cancelled in, and Plex already records it
  (`items.season`). Nothing about it is invented here. Four and a half seasons watched is `r = 4.5`.

**`sqrt` rather than a tunable damping constant.** Nine seasons of one show should count for more
than one film, but not nine times more. `sqrt` gives 3×. The alternative shapes carry a constant
— `1 + α·log(1 + r/ε)`, BM25's `k1` — and there is no published rule for choosing one: Hu, Koren
and Volinsky report `α = 40` "found to produce good results" on their data, the `implicit` library
ships `K1 = 100` for play counts, and *Introduction to Information Retrieval* recommends
`k1 ∈ [1.2, 2]` for text. Three domains, three numbers, each tuned against its own dataset. A
constant nobody can derive is a constant nobody can tell has gone stale. `sqrt` has none, and
`sqrt(1) = 1` keeps one full pass anchored at weight 1.

**No recency half-life, because it stopped helping once the unit was right.** Measured on one real
account, the top keyword's share of the vector: no decay 13.6%, 180-day 17.8%, 90-day 20.3%,
30-day 22.4%. Every half-life *concentrated* the vector on one recent show rather than mixing it.
The apparent need for decay in earlier passes was compensating for a unit that over-counted long
shows; correcting the unit removed the reason for the knob. A half-life would have to earn its
place on evidence, and this evidence points the other way.

**Abandonment is no signal, not negative signal.** `r < 0.5` contributes nothing, which is
Hu/Koren/Volinsky §6 verbatim: *"we toggle to zero all entries with r_ui < 0.5, as watching less
than half of a program is not a strong indication that a user likes the program."* They zero it;
they do not flip it. Netflix's 2024 write-up on long-term satisfaction marks a ten-minute partial
watch "ambiguous", not a dislike. A survey of published work found no system that scores an
abandoned watch as negative. On real data the rule drops 18 of 99 titles, and they read exactly
like things someone bailed on: three episodes of four different shows, two of another.

**The exploration fraction is not a property of a taste vector at all**, which is why it has no
value here. It describes how much of a *channel* is deliberately off-profile — a decision about
assembling a lineup, made by whatever is building the lineup. The rollup reports what someone
likes; it has no opinion on how much of a channel should be something else.

## Consequences

**The rollup ships no policy knobs, so the ownership question in #13 dissolves.** There is nothing
to own: no half-life, no negative weight, no damping constant. `plexdb-reader`'s `taste_vector_for`
stays what ADR-0003 said it was — a report, not a ranker. The one remaining knob, the exploration
fraction, is the consumer's and belongs in a channel's own config.

**A season boundary now matters to identity.** `items.season` was previously carried for display;
the rollup depends on it. A show whose episodes Plex files without a season number falls back to
counting each play as one unit, which over-weights it. That is a known gap, not a designed
behaviour.

**Rewatching pushes `r` past 1 and is not capped.** Watching a season twice is `r = 2`, damped to
1.41. This is deliberate — Hu/Koren/Volinsky treat repeat consumption as the strongest positive
signal they have — but it means a comfort rewatch outweighs a first-time watch of something new,
which is worth revisiting if exploration ever feels starved.

**Keyword mass is split per title.** A title contributes its weight *divided across its keywords*,
so a show TMDB tagged with twenty keywords does not outvote one tagged with five. Without this, a
vector's top entries are whichever show happened to be tagged most heavily.

## Evidence

The comparison runs this was decided from are throwaway scripts under `tmp/claude/`, kept out of
the package deliberately — the reader crate carries no ranking policy, so the decay and damping
they compare exist nowhere in shipped code. The research behind the citations above, with the
formulas quoted and two unverifiable claims retracted, is in the same directory.
