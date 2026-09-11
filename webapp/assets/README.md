# Fonts

## Bagnard.woff2

The IndustriousAF wordmark face. Used only by the wordmark on the dashboard;
nothing else in this interface sets type in it.

Copyright (c) 2015 Sebastien Sanfilippo (www.love-letters.be), licensed under
the SIL Open Font License v1.1. The full licence is in `OFL.txt` beside this
file and must stay there: the OFL permits redistribution inside a larger work
like this one, but only with the copyright notice and licence attached.

No Reserved Font Name is declared, so the family keeps its own name here.

Two notes for anyone reconciling this against the IndustriousAF design system:

- The design system credits Bagnard to Sun Young Oh. The font's own licence
  names Sebastien Sanfilippo. Sun Young Oh designed Anthony for Velvetyne,
  which is a different face.
- The file is 7.2 KB and is NOT subsetted. It carries the full Latin
  alphabet, lowercase and digits, which was checked by rendering rather than
  assumed from the size.

`shared.py` reads this file at import and inlines it as a data URI, so the
interface serves no static routes and works with no network.
