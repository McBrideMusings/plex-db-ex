#!/usr/bin/env python3
"""Print the title, artist and featured artists of a YouTube link as one JSON object.

Takes the URL as its only argument. Asks YouTube's oEmbed endpoint for the video's title
and channel; downloads no media. Standard library only.
"""

import json
import re
import sys
import urllib.parse
import urllib.request

# "(Official Video)", "[Official Music Video]", "(Lyric Video)", "(HD)" and the like.
TAG = re.compile(
    r"\s*[\(\[]\s*(?:official|lyric|lyrics|music|hd|4k|remastered|audio|video)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
FEATURING = re.compile(
    r"\s*[\(\[]?\s*(?:ft\.?|feat\.?|featuring)\s+([^\)\]]+?)\s*[\)\]]?\s*$", re.IGNORECASE
)


def scrape(url: str) -> dict:
    query = urllib.parse.urlencode({"url": url, "format": "json"})
    with urllib.request.urlopen(f"https://www.youtube.com/oembed?{query}", timeout=15) as reply:
        info = json.load(reply)
    title = TAG.sub("", info.get("title", "")).strip()
    out = {}
    featured = FEATURING.search(title)
    if featured:
        out["featured"] = featured.group(1).strip()
        title = title[: featured.start()].strip()
    artist, dash, rest = title.partition(" - ")
    if dash and artist.strip() and rest.strip():
        out["artist"] = artist.strip()
        title = rest.strip()
    if title:
        out["title"] = title
    return out


if __name__ == "__main__":
    print(json.dumps(scrape(sys.argv[1])))
