# Skill Name: Web Frontend

## Description
Conventions for building HTML, CSS, and JavaScript pages: semantic markup,
external stylesheets and scripts, responsive layout, accessibility, and vanilla
(offline-friendly) code.

## Trigger Keywords
html, css, js, javascript, web, webpage, website, page, landing page, frontend,
front-end, ui, layout, responsive, navbar, footer, hero, form, styling, dom, site

## Instructions
When building or editing a web page:
1. Use semantic HTML5 elements (header, nav, main, section, article, footer)
   instead of generic <div> soup.
2. Keep CSS in an external styles.css and JS in an external script.js; link them
   with <link rel="stylesheet"> and <script src>. Do not inline large blocks.
   Every file you reference MUST also be created so the page actually works.
3. Responsive by default: mobile-first, flexbox or grid, relative units
   (rem/%/vw), and a <meta name="viewport"> tag.
4. Accessibility: alt text on every <img>, a <label> for every input, meaningful
   link text, and adequate color contrast.
5. Vanilla by default: no external CDNs or frameworks unless explicitly asked —
   it must work offline.
6. Put only real code in each file — no prose, explanations, or "here is..."
   commentary inside the file itself.
