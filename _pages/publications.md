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

<input type="text" id="bibsearch" class="form-control" placeholder="Type to filter" onkeyup="filterPublications()">

<div class="publications">

<div class="pub-section">
<h3>International Conferences and Workshops</h3>
  {% bibliography -f papers -q @*[peer=true]* %}
</div>

<div class="pub-section">
<h3>Pre-prints</h3>
  {% bibliography -f papers -q @*[preprint=true]* %}
</div>

<div class="pub-section">
<h3>Book Chapters</h3>
  {% bibliography -f papers -q @*[book=true]* %}
</div>

<div class="pub-section">
<h3>Posters</h3>
  {% bibliography -f papers -q @*[poster=true]* %}
</div>

<div class="pub-section">
<h3>Theses</h3>
  {% bibliography -f papers -q @*[thesis=true]* %}
</div>

</div>

<script>
  function filterPublications() {
    var query = document.getElementById('bibsearch').value.trim().toLowerCase();
    document.querySelectorAll('.pub-section').forEach(function (section) {
      var visibleCount = 0;
      section.querySelectorAll('.bibliography > li').forEach(function (entry) {
        var matches = entry.textContent.toLowerCase().indexOf(query) !== -1;
        entry.style.display = matches ? '' : 'none';
        if (matches) visibleCount++;
      });
      section.style.display = (query === '' || visibleCount > 0) ? '' : 'none';
    });
  }
</script>
