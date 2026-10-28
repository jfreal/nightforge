---
name: postiz-post
description: Post a finished video to every channel in a Postiz group, such as "Personal", through the Postiz MCP. Makes a small upload copy, writes copy tailored to each channel (X, LinkedIn, YouTube, TikTok and the rest), gets the user's approval, uploads through the Postiz upload widget, and publishes or schedules all channels in one call. Use when the user wants to post, publish, share, cross-post or schedule a video or short to their socials, a Postiz group, or "all my channels", or invokes /postiz-post.
---

# Postiz post

Takes one finished file and puts it on every channel in a Postiz group. The edit itself is
another skill's job (`short-form-video-edit`, `video-logo-intro`). This skill starts from a
file that is ready to watch.

The Postiz MCP tools share one server prefix (it looks like `mcp__<uuid>__`). They are
usually deferred: load the ones below with one `ToolSearch` `select:` call before step 1.

| Tool | Used for |
|---|---|
| `groupList`, `integrationList` | the group and the channels in it |
| `integrationSchema` | each platform's rules and required settings |
| `uploadWidgetTool`, `uploadWidgetStatusTool` | getting a local file into the media library |
| `uploadFromUrlTool`, `mediaStatusTool` | the same, when the file is already on a public URL |
| `integrationSchedulePostTool` | posting: now, scheduled, or as a draft |

## Workflow

1. **Find the channels.** `groupList` gives the group's id. `integrationList` gives every
   channel; keep those in the group. Note each channel's integration id, platform
   identifier (`x`, `linkedin`, `youtube`, `tiktok-business`, ...) and display name. Tell
   the user which channels you found. If a channel is disabled or needs reconnecting, say
   so now.
2. **Read each platform's schema.** Call `integrationSchema` once per platform, with
   `isPremium: false` unless the user has X Premium. Note the length limit, whether media
   is required, and the required settings. See [Platform notes](#platform-notes) for
   settings that bit us before.
3. **Make the upload copy.** Never upload the edit master, which is often 150 MB or more.
   Re-encode a copy next to it as `<name>-upload.mp4`:

   ```bash
   input="path/to/finished.mp4"
   output="${input%.*}-upload.mp4"
   ffmpeg -i "$input" -c:v libx264 -crf 23 -preset slow -maxrate 6M -bufsize 12M \
     -pix_fmt yuv420p -c:a aac -b:a 160k -movflags +faststart "$output"
   ```

   A 72 s 1080x1920 short came out at 53 MB this way. Check it with `ffprobe`: h264,
   yuv420p, AAC, same length as the master.
4. **Write the copy, one version per channel.** Run it through the `unslop` skill. Lead with
   the hook from the video. Fit each platform:
   - **X:** at most 280 characters, including spaces. Count them. The hook, the proof, the
     punchline.
   - **LinkedIn:** the long version. One idea per short paragraph, the story in order, the
     lesson last, two or three hashtags at the end.
   - **YouTube:** a title of at most 100 characters (end it with `#Shorts` for a short), the
     LinkedIn text as the description, and five or so tags.
   - **TikTok:** a title of at most 90 characters and a one-line caption with hashtags.
5. **Human checkpoint.** Show the user every channel's text and settings: visibility,
   comments, who can reply, made-for-kids, and the time (now, or a scheduled time shown in
   both the user's timezone and UTC). Posting publishes to public accounts, so wait for a
   clear yes. The yes covers the text and settings exactly as shown. If anything changes
   after that, even a setting a validation error forces you to add, show the final value and
   ask again.
6. **Upload.** Call `uploadWidgetTool`. It returns a `sessionId` and shows the user an upload
   box. Tell them which file to pick, then wait for them to say it finished. Check with
   `uploadWidgetStatusTool` (or `read_widget_context` on the widget) until the file shows
   `"status":"ready"` with a `path` on `https://uploads.postiz.com/`. If it says
   `processing`, wait about 10 s and check again.
7. **Post.** One `integrationSchedulePostTool` call with one `socialPost` item per channel.
   Every item gets its own `settings`, `shortLink: false` unless there is a link, a `type`,
   and a `postsAndComments` array. The array's first entry is the post: the upload `path` in
   `attachments` and the text in `content`. Any further entries are comments. The `type` is:
   - `now`: pass the current UTC time as `date`. Get it with `date -u +%Y-%m-%dT%H:%M:%SZ`,
     never from memory.
   - `schedule`: the user's time, converted to UTC. Ask for the timezone (an IANA name such
     as `America/New_York`) or confirm the one you inferred before converting.
   - `draft`: when the user wants to finish it in the Postiz app.

   `content` is HTML: every paragraph in its own `<p>`. If the result has `output.errors`,
   nothing was posted. Fix the setting it names, show the user the new value for a yes (step
   5), and send the whole call again.
8. **Report.** List each channel with its `previewUrl`, and say what Postiz did for that
   `type`: accepted for publishing now, scheduled for the UTC time, or saved as a draft that
   publishes nothing until the user sends it from Postiz. For `now` and `schedule`, Postiz
   publishes in the background. YouTube and TikTok take a few minutes to
   process video, and a failure shows on the post in Postiz, not in this tool's result.

## Platform notes

Read `integrationSchema` anyway. These are the settings that failed or were easy to get wrong.

| Platform | Notes |
|---|---|
| X | Needs `who_can_reply_post`: `everyone`, `following`, `mentionedUsers`, `subscribers` or `verified`. The schema did not show it as required, but the post call rejected the batch without it. One video at most. |
| LinkedIn | One video at most. No required settings. |
| YouTube | `title` (2 to 100 characters) and `type` (`public`, `private`, `unlisted`) are required. `selfDeclaredMadeForKids` is `yes`/`no`. `tags` is a list of `{value, label}`. |
| TikTok Business | Required: `privacy_level`, `duet`, `stitch`, `comment`, `autoAddMusic`, `brand_content_toggle`, `brand_organic_toggle`, `content_posting_method`. Use `DIRECT_POST`. `UPLOAD` only puts a draft in the TikTok app inbox and drops every other setting. `privacy_level` and `autoAddMusic` are still required for a video even though TikTok ignores them there; send `PUBLIC_TO_EVERYONE` and `no`. `title` at most 90 characters. |

## If it goes wrong

| Problem | Fix |
|---|---|
| `mediaListTool` returns `{"pages":0,"output":[]}` even though the user sees the file in the Postiz web app | The library list did not show files uploaded in the web app. Don't rely on it. Open a fresh `uploadWidgetTool` session and have the user upload there. |
| No upload box appears | The app does not show MCP Apps widgets. Ask the user to copy the file's link from the Postiz web media library (`https://uploads.postiz.com/...`) and use it as the attachment. Failing that, the user runs the Postiz CLI (`postiz upload`) themself. It needs their API key, which you never handle. |
| The video is on a public URL already | `uploadFromUrlTool` with that URL. Pass `adjust: true` only if a platform rejected the file's size or encoding, then wait for `mediaStatusTool` to say `ready`. |
| `SendUserFile` refuses the video | It caps files at 30 MiB for phone viewers. Send the contact sheet instead and give the path to the video. |
| The call returns `output.errors` | Nothing went out. Fix the named setting, get the user's yes on it, and resend all channels in one call. Don't resend only the failed one, or the posts go out at different times. |
