# Newspaper serif fallback

`noto-serif-sc-regular.woff2` is Noto Serif SC Regular (weight 400), converted from the unmodified Google Fonts TrueType distribution to WOFF2 with FontTools. All glyphs are retained. The accompanying `OFL.txt` is the SIL Open Font License 1.1.

Source: https://fonts.gstatic.com/s/notoserifsc/v36/H4cyBXePl9DZ0Xe7gG9cyOj7uK2-n-D2rd4FY7SCqyWv.ttf
License: https://github.com/google/fonts/blob/main/ofl/notoserifsc/OFL.txt

Latin text and digits use Times New Roman or another installed Western serif before any Chinese face. Lining numerals keep digits on a consistent baseline. The `Newspaper Song` face is restricted to Han characters and CJK punctuation with `unicode-range`; it cannot supply ASCII letters or digits. CSS prefers an installed Chinese serif face and uses this same-origin fallback when one is unavailable. `font-display: swap` keeps text readable while it loads; bold is synthesized by the browser. No third-party font request is made by readers.
