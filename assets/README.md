# Icônes

Ces icônes proviennent du thème d'icônes **Papirus-Dark** (noms freedesktop
`input-keyboard` et `input-mouse`), utilisés par SwayOSD sur la machine de
développement.

- Source : https://github.com/PapirusDevelopmentTeam/papirus-icon-theme
- Copyright : Papirus Development Team
- Licence : GNU General Public License v3.0 (voir `COPYING`)

Fichiers concernés :

- `keyboard.svg` / `mouse.svg` : sources vectorielles (copiées depuis
  `Papirus-Dark/64x64/devices/`).
- `keyboard.png` / `mouse.png` : versions rasterisées 128×128 utilisées par les
  toasts Windows.

Windows ne sait pas afficher de SVG dans les toasts : le rendu doit être PNG
(ou JPG/GIF/BMP). Les SVG sont conservés comme source et les PNG peuvent être
régénérés avec :

```sh
rsvg-convert -w 128 -h 128 assets/keyboard.svg -o assets/keyboard.png
rsvg-convert -w 128 -h 128 assets/mouse.svg -o assets/mouse.png
```
