FABLE Scenathon Trade Add-in assets

Files
-----
fable-logo-full.png  -> full FABLE Consortium logo for the task pane header
icon-16.png          -> ribbon/add-in icon
icon-32.png          -> ribbon/add-in icon
icon-64.png          -> high-resolution add-in icon
icon-80.png          -> Office ribbon high-DPI icon
icon-128.png         -> future marketplace/listing use
icon-master.png      -> transparent master emblem

Where to copy
-------------
Copy these files into:
C:\Users\JAHID\FABLE Scenathon Trade\assets\

You can replace files with the same names already present in that folder.

Next step
---------
After copying the assets, update taskpane.html to use:
../assets/fable-logo-full.png

Then verify manifest.xml points to:
https://localhost:3000/assets/icon-16.png
https://localhost:3000/assets/icon-32.png
https://localhost:3000/assets/icon-64.png
https://localhost:3000/assets/icon-80.png

Do not change taskpane.ts for branding.
