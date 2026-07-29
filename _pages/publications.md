---
layout: page
permalink: /publications/
title: Publications
description: The complete list of my works in reversed chronological order.
nav: true
nav_order: 2
---
<a href="https://scholar.google.com/citations?user=Na1MX7EAAAAJ&hl=en"><code class="language-plaintext highlighter-rouge">Google Scholar</code></a>
<!-- _pages/publications.md -->
<div class="publications">

<h3>International Conferences and Workshops</h3>
  {% bibliography -f papers -q @*[peer=true]* %}

<h3>Pre-prints</h3>
  {% bibliography -f papers -q @*[preprint=true]* %}

<h3>Book Chapters</h3>
  {% bibliography -f papers -q @*[book=true]* %}

<h3>Posters</h3>
  {% bibliography -f papers -q @*[poster=true]* %}

<h3>Theses</h3>
  {% bibliography -f papers -q @*[thesis=true]* %}

</div>
