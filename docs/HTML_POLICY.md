# HTML policy

`panda.handlers.html` turns any HTML page into a review transcript in a closed output
dialect. The output keeps the page's reading text and nothing else, so retained text
can never become markup, a link or an executable surface.

## Reading the source

The source is parsed with BeautifulSoup's `html.parser`, after a markup-size check. A
page is refused (`E_MALFORMED`) if decoding needed replacement characters, or if any
text block contains a NUL, U+FFFD, or percent- or backslash-escapes (`%41`, `\x41`,
`A`). Detection can't see through those encodings, so text using them can't be
verified.

The tree is walked iteratively, so deep nesting can't hit a recursion limit.

- **Dropped with their whole subtree:** `head title script style template noscript
  iframe frame frameset object embed applet svg math canvas video audio img picture
  source track map area meta link base param dialog slot`.
- **Inline (text joins the current block):** `a abbr b bdi bdo cite code del em i ins
  kbd mark q s samp small span strong sub sup time u var label font nobr wbr big tt
  data output`.
- **Form controls:** the `value` attribute of `input`, `option` and `button` is kept as
  text, because it is the submitted content. Values of `hidden` and `password` inputs
  are not.
- **`br` and `hr`** end a block.
- **Every other element**, including unknown ones such as `<form>`, starts and ends a
  block and keeps its text. Dropping unknown containers silently deleted whole web-form
  exports.

Attributes, comments, doctype and processing instructions are never emitted.

## Output dialect

Each block is sanitised with the previous block as detection context (a label in one
table cell, its value in the next), then written as:

```
<html><body><p>escaped text</p><p>escaped text</p>...</body></html>
```

The text is UTF-8, escaped with `html.escape(..., quote=False)`. No other element,
attribute or whitespace appears outside the paragraphs.

## Output validation

`extract_html_markup_units`, used by the leak check and for HTML inside EML, accepts
only this exact form. It does not parse tolerantly. A single trailing line ending is
allowed, because MIME text decoding can add one. Anything else refuses the file:

- the envelope is exactly `<html><body>` ... `</body></html>`
- the content is a run of `<p>...</p>` with no `<` inside a paragraph
- each paragraph's text re-escapes byte for byte to what is on disk (canonical escaping)
- no paragraph decodes to a NUL, U+FFFD or a percent- or backslash-escape

Each paragraph is scanned as its own unit, `html/text/N`.
