# VRM Animation Assets

This directory contains FBX animation files for the VRM avatar system.

## Animation System

The SyntH animation system automatically manages avatar animations throughout the message processing lifecycle. The system is centrally managed by the `AnimationHandler` in `core/animation_handler.py` and coordinates with the WebUI frontend.

### Animation States

The system defines these logical animation states (the first four are the conversation's; `touch` and
`skin_change` are reactions; `sit`, `crouch` and `gaming` are what a body with legs and hands is doing):

- **Idle**: Default state when no activity is occurring (files: `Idle.fbx`, `Idle2.fbx`, `Happy Idle.fbx`)
- **Think**: Triggered when a message is received (files: `Thinking.fbx`)
- **Write**: Triggered when the LLM starts generating a response (files: `Texting While Standing.fbx`, `Texting.fbx`)
- **Talk**: Can be triggered for speech output (files: `talking.fbx`)
- **Sit** (`sit/`): sitting down. `sit/Sitting Idle.fbx` loops. A bed is not a chair: the legs-swinging clip sits in
  `sit/bed/` (a sub-folder is a variant the rotation never picks, like `idle/happy/`) until something can say "on a bed".
- **Crouch** (`crouch/`): crouching, e.g. to reach something on the floor. `Crouching Idle.fbx` loops;
  `Crouch To Stand.fbx` is `play_once`, i.e. a **post** clip: how the state ENDS (a client that can plays it when the
  state is left; the rotation never picks it).
- **Gaming** (`gaming/`): playing with a controller. `Gaming-sitting.fbx` was captured seated, so its descriptor says
  `"mask": "upper_body"` (below): a client with a body shows only what the back, arms and hands do, whether she stands,
  walks or sits. A client that cannot layer plays the whole clip, seated.

When multiple files are specified for a state, one is randomly selected each time the animation plays.

### Automatic Animation Flow

1. User sends message → **Think** animation plays (looping)
2. LLM starts responding → **Write** animation plays (looping)
3. Response complete → **Idle** animation plays (looping)

No manual intervention is required - animations are automatically triggered by the backend.

For more information, see the [Animation System Documentation](../../docs/animation_system.rst).

## Descriptor Format (.fbx.json)

Each `.fbx` animation file should have an optional adjacent descriptor `.fbx.json` that configures animation properties, facial expressions, blinking behavior, and eye movement.

### Example: Full Descriptor

```json
{
  "intro": { "start_frame": 0, "end_frame": 15 },
  "loop": { "start_frame": 16, "end_frame": 60 },
  "outro": { "start_frame": 61, "end_frame": 90 },
  "fps": 30,
  "play_once": false,
  "lipsync": false,
  "expressions": [
    {
      "start_frame": 0,
      "end_frame": 15,
      "targets": { "eyes_closed": 0.1, "mouth.O": 0.05 },
      "priority": 10,
      "source": "server"
    }
  ],
  "blink": {
    "auto": true,
    "rate_s": 3.5,
    "intensity": 0.6,
    "close_ms": 60,
    "hold_ms": 120,
    "open_ms": 60
  },
  "eye_movement": {
    "auto": true,
    "saccade_rate_s": 2.0
  }
}
```

### Field Descriptions

**Timing Fields:**
- `intro`: Optional intro section with `start_frame` and `end_frame` (plays once at animation start)
- `loop`: Main looping section with `start_frame` and `end_frame` (repeats while animation is active)
- `outro`: Optional outro section (plays once when transitioning away)
- `fps`: Frames per second of the FBX (default: 30)
- `play_once`: If true, animation plays once and does not loop (good for transitions)

**Layer field:**
- `mask`: `"upper_body"` = the clip drives only the spine up (back, neck, head, shoulders, arms, hands, fingers).
  The hips and legs go on with whatever else is playing. Unknown values and clients that cannot layer ignore it and
  play the whole clip. Posture (sitting, standing) and activity (playing, typing) are then two things, not one.

**Facial Expression Fields:**
- `lipsync`: Boolean flag (default: false). If true, animation is suitable for lip-sync synthesis
- `expressions`: Array of expression objects with:
  - `start_frame`, `end_frame`: Frame range for this expression
  - `targets`: Object mapping logical blendshape names to intensity (0.0-1.0). Names resolve via `skins/<skin>/persona.json` → `blendshape_map`
  - `priority`: Numeric priority (higher = applied later, can override lower priority expressions)
  - `source`: String identifying origin ("server", "descriptor", "persona_override", etc.)

**Blink Control:**
- `blink.auto`: If true, enable autonomous blinking during this animation
- `blink.rate_s`: Average blink rate in seconds (default: 3.5)
- `blink.intensity`: Blink intensity (0.0-1.0, default: 0.6)
- `blink.close_ms`: Time to close eyes in milliseconds (default: 60)
- `blink.hold_ms`: Time to keep eyes closed (default: 120)
- `blink.open_ms`: Time to open eyes (default: 60)

**Eye Movement Control:**
- `eye_movement.auto`: If true, enable autonomous saccades (eye look movements) during this animation
- `eye_movement.saccade_rate_s`: Average time between saccades in seconds (default: 2.0)

### Smart Eye-Closed Behavior

When expressions intentionally close the avatar's eyes (via `eyes_closed` blendshape > 0.5):
- **Blink is automatically suspended** during the eye-closed state
- **Eye movement (saccades) is automatically suspended** during the eye-closed state
- **Both automatically resume** when the eyes are reopened by expressions

This prevents conflicting animations: while eyes are intentionally closed, blinking and saccades remain paused until the eyes are reopened. This applies automatically - no configuration needed.

New: the WebUI now exposes a rich `eyesState` mechanism and a DOM event `synth_eyes_state_changed`.
- `synth_eyes_state_changed` fires with detail `{ value, source }` when the effective eyes closed state changes.
- Sources: `persona`, `animation`, `expression` (persistent) or `autoblink` (transient).
- The handler will lock blinking/eye movement when a persistent source sets `value > 0.5`, and will automatically resume when cleared.

**Implementation:**
 - Execution-time check in `_performBlink()`: skips blink if `eyes_closed > 0.5`
- Frame-time monitoring in `applyExpressionsForFrame()`: monitors eye state every frame and auto-suspends/resumes loops

### Making a clip loop (the `loop` window)

A clip with `intro`/`loop`/`outro` plays its intro once, then repeats the **loop** window until the state changes.
`intro.end_frame` = `loop.start_frame` and `loop.end_frame` = `outro.start_frame`: the three are one run of frames.

A held pose is not a cycle: it **drifts** (Thinking's head is 4 degrees and its arm 3 away from where they were a second
earlier). Cutting the window and repeating it steps by that much every repeat, and the mean angle over the bones hides it
(0.7 deg/bone for a step of 4 on one bone): judge a window by its **worst bone**. Two ways to a smooth repeat:

1. Find a window whose ends meet. `RetroXR/mods/xenu.synth_rift/tests/loop_scan.gd` prints a clip's speed profile (a held
   pose is where it nearly stands still) and the best windows by length:
   `godot --headless --path RetroXR --script res://mods/xenu.synth_rift/tests/loop_scan.gd -- clip.fbx 20 --within=40,90`
2. When none does (Thinking: nothing better than 2 degrees), `"loop": {..., "blend_frames": N}` asks the client to build the
   repeat: the window's last N frames are blended toward the N frames before its start, so its end IS its start. The
   blend must come from a still part of the clip (not the arrival, which moves the arm 20 degrees). A client that does
   not know the field cuts the window, as before.

Thinking: held between frames ~46 and ~74; `loop` 54..74 with `blend_frames` 16 (a step of 0.37 deg per 30 fps frame at its
worst; 43..73 over 12 frames was 2.8). A clip that is already a cycle (Walking, Sitting Idle, Gaming, Crouching Idle:
first and last frame within 0.03 deg/bone) needs no window and no descriptor.

### Descriptor Notes

- All fields are optional; omit what you don't need
- Missing fields use defaults from `skins/<skin>/persona.json` → `defaults`
- `expressions.targets` keys are resolved through `blendshape_map` in the skin's `persona.json`
- For transition animations (with `play_once: true`), consider setting `blink.auto: false` and `eye_movement.auto: false` to avoid distraction

## Available Animations

### Idle Animations

#### Idle.fbx
- **Purpose**: Basic idle animation
- **Duration**: Looping
- **Use Case**: Default idle state
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig

#### Idle2.fbx
- **Purpose**: Alternate idle animation
- **Duration**: Looping
- **Use Case**: Randomly selected idle state for variety
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig

#### Happy Idle.fbx
- **Purpose**: Cheerful idle animation
- **Duration**: Looping
- **Use Case**: Positive/upbeat idle state
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig

### Processing Animations

#### Thinking.fbx
- **Purpose**: Contemplative thinking animation
- **Duration**: Looping
- **Use Case**: Played when processing incoming messages

#### Texting While Standing.fbx
- **Purpose**: Standing typing animation
- **Duration**: Looping
- **Use Case**: Randomly selected when generating responses
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig with emphasis on arms/hands

#### Texting.fbx
- **Duration**: Looping
- **Use Case**: Randomly selected when generating responses
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig with emphasis on arms/hands

### Communication Animations

#### talking.fbx
- **Purpose**: Speaking animation
`animations/<state>/` folder for the active skin; the `AnimationHandler` will discover them
automatically. You can also register overrides or aliases from plugins using the public
APIs `register_state_animations`, `register_state_aliases` and add custom search paths via
`set_animation_search_paths` in `core/animation_handler.py`.
- **Use Case**: Played when the AI vocalizes or speaks
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig with emphasis on head/chest movement

### Emotion Animations

#### Angry.fbx
- **Purpose**: Angry/frustrated emotion
- **Duration**: One-shot or short loop
- **Use Case**: Reserved for future emotional response system
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig

### Movement Animations

#### Texting And Walking.fbx
- **Purpose**: Walking while typing
- **Duration**: Looping
- **Use Case**: Reserved for future multi-tasking animations
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig with lower body movement

## Adding New Animations

To add a new animation to the system:

### 1. Export from Mixamo

- Go to https://www.mixamo.com
- Select your desired animation
- Download in FBX format (.fbx)
- Use "Without Skin" option for better compatibility

### 2. Place in Directory

```bash
cp your_animation.fbx res/synth_webui/animations/
```

### 3. Update Backend Mapping

Simply drop your `.fbx` files (and optional `.fbx.json` descriptors) into the appropriate
`animations/<state>/` folder for the active skin; the `AnimationHandler` will discover them
automatically. You can also register overrides or aliases from plugins using the public
APIs `register_state_animations`, `register_state_aliases` and add custom search paths via
`set_animation_search_paths` in `core/animation_handler.py`.

To add a completely new state, update the `AnimationState` enum:

```python
class AnimationState(Enum):
    IDLE = "idle"
    THINK = "think"
    WRITE = "write"
    TALK = "talk"
    CUSTOM = "custom"  # New state
```

### 4. Update Frontend Mapping

Edit `res/synth_webui/js/vrm-viewer.mjs` and update `animationMappings`:

```javascript
const animationMappings = {
    think: ['Thinking.fbx'],
    write: ['Texting While Standing.fbx', 'Texting.fbx'],
    talk: ['talking.fbx'],
    idle: ['Idle.fbx', 'Idle2.fbx', 'Happy Idle.fbx', 'your_animation.fbx'],  // Add here
    custom: ['your_animation.fbx']  // Or add new state
};
```

### 5. Trigger from Code (Optional)

If you want to manually trigger the new animation from a component:

```python
from core.animation_handler import get_animation_handler, AnimationState

handler = get_animation_handler()
await handler.transition_to(
    AnimationState.CUSTOM,  # Your new state
    session_id="session_id",
    context_id="my_context"
)
```

### 6. Test

- Restart SyntH
- Open the WebUI
- Upload a VRM model
- Send a message or trigger your custom animation
- Check browser console for loading errors

## Implicit Descriptor Behavior

**Animations without a descriptor file (`.fbx.json`) are automatically handled with sensible defaults:**

### Default Behavior by State

| State | Behavior | Looping | Use Case |
|-------|----------|---------|----------|
| IDLE | Play 0→maxframes, loop | Yes | Continuous background animation |
| THINK | Play 0→maxframes, once | No | Transient thinking pose |
| WRITE | Play 0→maxframes, once | No | Transient writing pose |
| TALK | Play 0→maxframes, once | No | Transient speaking pose |

### Rationale

- **Zero Configuration**: Drop animations into state folders (`animations/think/`, `animations/write/`, etc.) and they work immediately
- **Smart Transitions**: Non-IDLE animations play once, automatically returning to IDLE when complete
- **Override Control**: Provide `.fbx.json` descriptors to customize behavior per animation

### Example: Adding Animations Without Descriptors

```bash
# Drop animations - system auto-handles them
skins/Rei/animations/think/MyThinking.fbx         # Auto: plays once
skins/Rei/animations/write/MyTyping.fbx           # Auto: plays once
skins/Rei/animations/idle/MyIdle.fbx              # Auto: loops continuously
```

### When to Use Descriptors

Create `.fbx.json` descriptors when you need:
- **Structured sections** (intro→loop→outro)
- **Facial expressions** tied to animation frames
- **Custom blink/eye behavior**
- **Play-once behavior** for IDLE animations
- **Lip-sync support** flag

Example: `MyThinking.fbx.json`
```json
{
  "intro": {"start_frame": 0, "end_frame": 15},
  "loop": {"start_frame": 16, "end_frame": 50},
  "outro": {"start_frame": 51, "end_frame": 75},
  "fps": 30
}
```

## Animation Requirements

- **Format**: FBX 7.4 or later
- **Rig**: Mixamo humanoid skeleton
- **Bones**: Must include standard humanoid bones (hips, spine, chest, neck, head, etc.)
- **File Size**: Keep under 5MB for optimal loading times
- **Duration**: 2-10 seconds recommended for looping animations

## Technical Notes

### Bone Mapping

Animations are automatically retargeted from Mixamo rig to VRM humanoid bones using the mapping defined in `res/synth_webui/js/mixamoVRMRigMap.js`.

### Animation Conversion

The `loadMixamoAnimation.js` utility handles:
- Quaternion rotation retargeting
- Hip height adjustment for different model scales
- VRM metaVersion compatibility (v0 and v1)

### Performance

- Animations are cached after first load
- AnimationMixer updates run at 60fps
- Crossfade transitions take 0.5 seconds

## Troubleshooting

### Animation Not Loading

**Error**: "Failed to load FBX"
- **Solution**: Verify file exists at correct path
- **Check**: File permissions are readable by web server

### Animation Looks Wrong

**Error**: Model stretches or rotates incorrectly
- **Solution**: Ensure FBX was exported "Without Skin"
- **Check**: VRM model has complete humanoid bone structure

### Performance Issues

**Error**: Choppy animation playback
- **Solution**: Reduce FBX file complexity
- **Check**: Use fewer keyframes (every 2-3 frames is sufficient)

## Future Animations

Planned additions:
- `thinking.fbx` - Contemplative pose for processing state
- `excited.fbx` - Enthusiastic response animation
- `sad.fbx` - Emotional response for negative content

Note: expression and viseme mappings for this skin are stored in `skins/Rei/persona.json` under the `blendshape_map` key. Use that file to tune emotion→blendshape, viseme→blendshape and per-animation presets.
- `gesture_*.fbx` - Hand gestures for emphasis

## Descriptor example (expressions + blink + lipsync)

Place a descriptor next to your FBX file (same name + `.json`), e.g. `Thinking.fbx.json`:

```json
{
    "loop": { "start_frame": 0, "end_frame": 60 },
    "expressions": [
        {
            "start_frame": 0,
            "end_frame": 15,
            "targets": { "eyes_closed": 0.4, "mouth.O": 0.05 },
            "source": "server",
            "priority": 10
        }
    ],
    "blink": { "auto": true, "rate_s": 4, "intensity": 0.6 },
    "eye_movement": { "auto": true, "saccade_rate_s": 2 },
    "lipsync": false
}
```

The WebUI will resolve targets through `skins/<skin>/persona.json` (`blendshape_map`).

## License

Animation files from Mixamo are subject to Adobe's Mixamo Terms of Use.
Custom animations should include appropriate licensing information.

## See Also

- [VRM Animation Documentation](../../docs/vrm_animations.rst)
- [Mixamo Animation Library](https://www.mixamo.com)
- [Three.js Animation System](https://threejs.org/docs/#manual/en/introduction/Animation-system)
