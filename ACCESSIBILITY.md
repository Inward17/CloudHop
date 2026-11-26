# Accessibility

Cloudhop's dashboard should be usable by everyone who manages apps with it, whatever device, input method or assistive
technology they use. This page says what we do today, where we know we fall short, and how to tell us about barriers.

## Our commitment

- We aim for [WCAG 2.2](https://www.w3.org/TR/WCAG22/) level AA for the dashboard and the demo.
- Accessibility problems are bugs. Reports that block someone from completing a task are treated as high priority.
- New dashboard features are checked at phone width, in light and dark themes, and with the keyboard before they ship.

## Supported environments

- **Browsers:** current Chrome, Edge and Firefox, and Safari 15.4 or later.
- **Screens:** phones from 320 px wide, tablets, laptops and large screens.
- **Themes:** light, dark, or following the device setting.

## What is in place

- Every action is a native button, link, form field or `<dialog>`, so it can be reached and used with a keyboard.
  Focused controls show a visible outline.
- Icon-only buttons and charts have text labels for screen readers. Status messages are announced through a polite live region.
- Every chart has a table view with the same numbers, so no information depends on reading a chart.
- Animations are turned off when the device asks for reduced motion.
- Touch screens get 44 px tap targets and 16 px text in form fields, so pages do not zoom unexpectedly.
- The dashboard uses Space Grotesk, served with the page, and sizes text in pixels from a 14 px base; browser zoom scales all of it. The system font is the fallback.

## Known limitations

- No formal WCAG audit has been done yet.
- The dashboard has not been tested end to end with screen readers (NVDA, JAWS, VoiceOver, TalkBack).
- Colour contrast has not been measured for every status colour and chart series in both themes.
- Long tables, such as logs, are not yet optimised for screen-reader navigation.
- Setup steps hand off to other sites (GitHub, AWS, Google Cloud, Vercel, Cloudflare), whose accessibility we do not control.

## Reporting a barrier

Open an issue using the bug report template and start the title with `Accessibility:`. Include:

- the page or feature, and what you were trying to do;
- your browser, device and any assistive technology (with versions, if you know them);
- what happened, and what you expected.

If you would rather not report publicly, ask the maintainer (Inward17 on GitHub) for a private channel. We aim to
reply within a week, and we will tell you if a fix will take longer.
