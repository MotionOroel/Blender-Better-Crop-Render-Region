Custom Crop Render Regions

Precisely control and scale your render crop regions numerically directly within Blender. Define your crop coordinates using either normalized fractions or concrete pixel coordinates, and watch the add-on dynamically adjust your internal render resolution to guarantee your target output dimensions and aspect ratios are perfectly maintained.

Includes object-bound target extraction, region presets, robust safety aspect locks, a persistent scene bookmarking engine, and an interactive modern 2D viewport overlay shader drawing system.
Key Features

    Dual-Coordinate Systems: Read, modify, and precisely assign your custom region via Normalized space Factors (0.0→1.0) or Absolute Resolution Pixels seamlessly.

    Dynamic Resolution Auto-Scaling: Adjusting your crop dimensions natively forces Blender to scale its underlying render viewport resolution so your cropped asset's final output file size exactly equals your target criteria without manual calculations.

    Aspect Ratio Isolation Constraints: Structural locks enforce fixed aspect ratio scaling, adapting widths or heights dynamically on adjustments based on target format requirements.

    Smart Bounds Extraction (Crop to Selection): Evaluates 3D bounding geometry matrices for active selections relative to the current active camera frustum, building a padded, safe mathematical crop around your targets instantly.

    Pre-bundled Crop Composition Presets: Rapidly inject industry standards (Social 9:16, Cinema 21:9, Square 1:1, Centers, Thirds) onto your framing composition with one click.

    Interactive Viewport Border Drawing: Features an active live structural render bounds bounding-box view overlay inside your viewport utilizing dedicated Blender GPU batch shaders (UNIFORM_COLOR).

    State Preservation Architecture: Automatically backs up native raw resolution vectors when toggled and restores baseline resolution fidelity cleanly upon add-on deactivation.

Installation

    Download the latest version source bundle (.zip format or clone the raw python file hierarchy).

    Launch Blender (Version 5.1.0 or newer required).

    Navigate to Edit → Preferences → Add-ons.

    Click Install... and navigate to your downloaded module.

    Search for Custom Crop Render Regions and click the checkbox to activate.

Interface Location

Find your workflow dashboard inside the native properties panel hierarchy:
Plaintext

Properties Workspace ➔ 📷 Render Properties ➔ ✂️ Custom Crop Regions Panel

Core Property Specifications

The core component operations manage multiple property hooks per scene state:
Property Reference	Subtype	Scope	System Objective
norm_x, norm_y	FACTOR	0.0 - 1.0	Normalised positioning coordinates for crop boundaries.
norm_w, norm_h	FACTOR	0.001 - 1.0	Normalised sizing dimensions for crop boundaries.
pixel_x, pixel_y	INTEGER	≥0	Pixel-offset positional coordinates for crop boundaries.
pixel_w, pixel_h	INTEGER	≥1	Pixel dimension sizing constraints relative to active frames.
target_w, target_h	INTEGER	≥1	Intended production export size criteria.
enabled	BOOLEAN	True/False	Master operational toggle.
lock_aspect	BOOLEAN	True/False	Aspect safety lock preservation toggle.
crop_to_border	BOOLEAN	True/False	Enforces structural output frame isolation.
Available Operators & Workflow Usage
📊 Base Control Operations

    Activate Crop: Enables dynamic crop hooks. It captures your default aspect size variables immediately, modifying target fields cleanly.

    Reset Frame (ccr.reset): Destroys all localized factor alterations, turns off border parsing buffers, and sets dimensions back to initial values.

    Match Active Scene Dimensions (ccr.match_scene): Pulls standard dimensions directly into your layout settings.

    Multiplier Scale Engine (ccr.target_multiplier): Multiplies output metrics safely up to 8× baseline metrics for extreme resolution renders.

📐 Automated Composition Framing

    Extract Geometry Bounds (ccr.crop_to_selection): Evaluates coordinates for selected meshes in your scene. It injects safety margins (auto_margin), locks orientation aspect variables (auto_fit_target_aspect), and sets up optimal framing based on what the active camera sees.

    Template Injections (ccr.apply_preset): Instantly structures composition frameworks:

        SQUARE_1_1 / SOCIAL_9_16 / CINEMA_21_9

        CENTER_25 / CENTER_50 / CENTER_75

        TOP_HALF / BOTTOM_HALF / LEFT_THIRD / RIGHT_THIRD

Technical Architecture Overview

The tool is built with performance-focused hooks to avoid render lag and protect your scene data:
Fragmento de código

graph TD
    A[User Modifies UI Prop / Pixel / Normalised] --> B{Module Update Guard Check}
    B -- _updating == True --> C[Drop Execution Stack / Prevent Loop]
    B -- _updating == False --> D[Set Guard Flag _updating = True]
    D --> E[Re-calculate Reciprocal Coordinate Arrays]
    E --> F[Evaluate Aspect Lock Matrix Adjustments]
    F --> G[Enforce Boundary Constraints inside 0.0-1.0 Factor space]
    G --> H[Update Native Blender Render Properties Engine]
    H --> I[Scale Blender Resolution Metrics dynamically based on Targets]
    I --> J[Release Guard Flag _updating = False]

    Infinite Loop Prevention: Uses a modular flag guard (_updating = True) across property sync states to stop feedback loops when translating between pixel maps and float values.

    GPU Overlay Performance: Uses Blender's modern gpu and blf modules. It loads the uniform color shader layout efficiently to display region borders instantly without performance hits.

License

This project is licensed under the MIT License - see the LICENSE file for details.
