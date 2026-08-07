# Design system

## Direction

A Sourcegraph research post, rendered as a self-contained dark reading surface. The page uses the current Sourcegraph blog type families and theme tokens. Its data visualizations follow the recent CodeScaleBench chart language: near-black fields, high-contrast direct labels, purple for the primary comparison, coral for rejected or unresolved evidence, and gray for context.

## Color

- Page background: `oklch(16% 0.00284 27deg)`
- Reading surface: `oklch(18% 0.00284 27deg)`
- Foreground: `oklch(94.5% 0.00284 27deg)`
- Sourcegraph brand coral: `oklch(71% 0.19 27deg)`
- Sourcegraph accent: `oklch(57% 0.2 265deg)`
- Blog chart purple: `#8552f2`
- Blog chart coral: `#ff7867`
- Blog chart black: `#020202`
- Blog chart grid: `#343434`
- Blog chart text: `#ededed`
- Blog chart muted text: `#a9a9a9`

Color is redundant with position, labels, or line treatment. Purple means accepted or primary evidence. Coral means rejected, unresolved, or a boundary. Gray shows the complete frame or comparison context.

## Typography

- Display headings: Perfectly Nineties, regular weight
- Body: Poly Sans
- Metadata and artifact paths: Poly Sans Mono
- Body measure: no more than 68 characters
- Charts: Poly Sans with direct value labels

All five Sourcegraph font files are embedded into the rendered HTML.

## Data visualization

- Lead with one sentence the chart can prove.
- Put the denominator in the chart title or subtitle.
- Label marks directly. Avoid legends when labels fit.
- Use a shared zero baseline for magnitude comparisons.
- Use bars for counts, a nested bar for attrition or follow-up, and a flow only for process stages.
- Do not present workflow categories as a population denominator.
- Keep caveats in the caption and expose the same values in a mobile text table.
- Avoid rounded chart cards, gradients, shadows, pictograms, and ornamental axes.

## Layout

The page uses a fixed research rail on wide screens and a horizontal navigation strip on smaller screens. Sections sit on one strict reading grid. Charts may use the full article width, while body copy stays narrow. Rules and spacing establish rhythm without enclosing every idea in a card.

## Responsive behavior

Below 48rem, charts may scroll horizontally, but every chart must also expose a visible non-SVG value list within the viewport. Navigation remains keyboard accessible and page width must never exceed the viewport.
