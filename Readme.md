# MCMI Tools

MCMI Tools is a modified version of [WWMI Tools](https://github.com/SpectrumQT/WWMI-Tools) for Wuthering Waves modding. It keeps the original import, export, extraction and template workflows, and adds practical improvements for Chinese users, LOD workflows, texture replacement and recent WWMI frame-dump changes.

Current version: `1.7.3.15`

## Installation

1. Download the source zip from the latest GitHub Release.
2. In Blender, open `Edit > Preferences > Add-ons > Install...`.
3. Select the downloaded zip and enable `MCMI Tools`.
4. Open the sidebar tab `MCMI Tools`.

If you already installed an older build, restart Blender after updating. The built-in updater checks GitHub Releases.

## Main Changes From WWMI Tools

### Chinese and English UI

The add-on panel and most common actions support Chinese and English labels. Change language from the add-on preferences.

Note: the default language is Chinese.

### LOD Import and Export

MCMI Tools can import LOD0, LOD1 and LOD2 object sources together and generate vertex-group mapping tables.

Basic workflow:

1. Enable `LOD`.
2. Set `Object Sources` to the LOD0 extracted folder.
3. Optionally set `LOD1 Sources` and `LOD2 Sources`.
4. Click `Import Object`.
5. Edit the LOD0 collection as usual.
6. Click `Export Mod`; the add-on exports LOD0 plus `LOD1` and `LOD2` subfolders when source folders are provided.

Notes:

- The mapping is generated from vertex-group centers and stored in the Blender text block `MCMI_LodMaps`.
- You can open and edit the mapping table from the LOD map editor before export.
- For merged skeleton workflows, the add-on temporarily duplicates and merges collections to calculate mappings.
- LOD export patches the main `mod.ini` so shared texture overrides can still work when LOD1/LOD2 objects are active.

### Targeted Frame-Dump Extraction

Use `Assign Hash` to extract only the object matching a specific IB hash. This is useful in crowded scenes where multiple compatible objects appear in the same frame dump.

Typical use:

1. In hunting mode, cycle the target IB hash.
2. Paste the 8-character hash into `Assign Hash`.
3. Run `Extract Frame Data`.

Leave `Assign Hash` empty to extract all compatible objects.

### Automatic Diffuse Texture Matching

The importer can connect a likely diffuse texture to each component automatically. This version no longer relies only on old slot and size assumptions.

Current matching behavior:

- candidate textures are filtered by SRGB format, square dimensions, and accepted sizes `1024x1024` or `2048x2048`;
- candidates are grouped by frame-analysis metadata from `TextureFormat.json`;
- if usage data is incomplete, component metadata is used as a fallback;
- UV islands are sampled on a fixed grid;
- candidate scores are based on RGB variance, weighted by each UV island's actual sample count;
- a global de-duplication pass avoids assigning the same texture to multiple components when another valid candidate exists;
- likely outline textures can be removed from later component selection;
- a diagnostic `DiffuseTextureMatch.log` is written beside the imported object sources.

Notes:

- The log is for debugging and is not needed in exported mods.
- The matcher is heuristic. If a character uses unusual material layouts, check `DiffuseTextureMatch.log` before assuming the selected texture is correct.
- Texture hashes added to the shading filter are excluded from automatic diffuse matching.

### Texture Hash Identity and Subfolders

Texture identity is based on the 8-character hash in the filename. The importer recursively searches `.dds`, `.png`, `.jpg` and `.jpeg` files under the object source folder.

Supported examples:

```text
9ac598b5.dds
C1-Diffuse-9ac598b5.dds
Textures/Body/C1-Diffuse-9ac598b5.dds
```

If multiple files contain the same hash, one is selected deterministically.

Export behavior:

- texture files are copied while preserving the imported relative subfolder and filename;
- generated extracted texture names remain pure hash names;
- `mod.ini` texture references keep the exported relative path.

### Texture Section Preservation

`Update Textures` controls whether export rewrites the generated `Shading: Textures` section in `mod.ini`.

Disable it when you manually edited texture overrides and only want to update mesh buffers or other generated sections.

### Extracted Resource Collection

Enable `Collect Extracted Resources` in debug settings to copy raw frame-dump resources used by the extracted object into an `ExtractResources` folder.

If extraction fails, resources are saved under `ExtractError/ExtractResources` when possible. This is intended for debugging and sharing a minimal repro.

### COLOR1 / TEXCOORD1 Handling

Some WWMI data stores UV-like data in `COLOR1`. MCMI Tools adjusts import and export handling so these layouts round-trip as `4UV + 1COLOR` where appropriate.

Use the toolbox action `Convert Vertex Colors` when working with older Blender files that still store color data in legacy vertex color layers.

### Optional Reverse Mod Extraction

If a `Reverse` folder with reverse tools exists beside the add-on, an extra extraction source mode appears for extracting object sources from an existing mod folder.

This helper is optional and not part of the core public release. Normal frame-dump extraction does not require it.

## GitHub Release Workflow

The add-on updater reads GitHub Releases from:

```text
https://github.com/ZelbertYQ/MCMI-Tools/releases
```

Version tags should match `bl_info["version"]`, for example:

```text
v1.7.3.15
```

The updater downloads GitHub's release source zip, so no separate update asset is required.

## Credits

Based on [WWMI Tools](https://github.com/SpectrumQT/WWMI-Tools).

Original and continued credits: SpectrumQT, LeoTorreZ, SinsOfSeven, SilentNightSound, DarkStarSword, ZelbertYQ.
