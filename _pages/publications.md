---
layout: page
permalink: /publications/
title: Publications
description: The complete list of my works in reversed chronological order.
years: [2022, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013]
nav: true
nav_order: 2
---
<a href="https://scholar.google.com/citations?user=Na1MX7EAAAAJ&hl=en"><code class="language-plaintext highlighter-rouge">Google Scholar</code></a>
<!-- _pages/publications.md -->
<div class="publications">

<!--
<h3>Pre-prints</h3>
  {% bibliography -f papers -q @*[preprint=true]* %}
-->

<h3>Peer-reviewed</h3>
  {% bibliography -f papers -q @*[peer=true]* %}

<h3>Book chapters</h3>
  {% bibliography -f papers -q @*[book=true]* %}

<h3>Workshops</h3>
  {% bibliography -f papers -q @*[workshop=true]* %}

<h3>Theses</h3>
  {% bibliography -f papers -q @*[thesis=true]* %}

</div>
