#!/usr/bin/env bash
# Generate disposable media using ffmpeg from the already pinned Jellyfin image.
set -euo pipefail
if [[ $# != 1 ]]; then
    echo 'Usage: make_clips.sh OUTPUT_DIRECTORY' >&2
    exit 2
fi
mkdir -p -- "$1"
output=$(cd -- "$1" && pwd)
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
image=$(sed -n 's/.*ServiceSpec("jellyfin", "\([^"]*\)".*/\1/p' "$repo/src/orama/catalog.py")
[[ -n "$image" ]]
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" --mount "type=bind,source=$output,target=/clips" \
    --entrypoint /bin/sh "$image" -eu -c '
    subtitle=$(mktemp)
    trap '\''rm -f -- "$subtitle"'\'' EXIT
    printf "1\n00:00:01,000 --> 00:00:04,000\nΔοκιμή Orama\n" > "$subtitle"
    clip() {
        /usr/lib/jellyfin-ffmpeg/ffmpeg -nostdin -hide_banner -loglevel error -y \
            -f lavfi -i "color=black:size=1920x1080:rate=24" \
            -f lavfi -i "anullsrc=channel_layout=5.1:sample_rate=48000" \
            -f srt -i "$subtitle" -map 0:v -map 1:a -map 2:s -t "$1" \
            -c:v libx264 -threads:v 2 -preset ultrafast -pix_fmt yuv420p \
            -c:a eac3 -b:a 448k -metadata:s:a:0 language=eng \
            -c:s srt -metadata:s:s:0 language=gre -disposition:s:0 0 "/clips/$2"
    }
    clip 120 Breaking.Bad.S01E01.1080p.WEB-DL.English.DDP5.1.H.264-ORAMA.mkv
    clip 180 Inception.2010.1080p.WEB-DL.English.DDP5.1.H.264-ORAMA.mkv
'
