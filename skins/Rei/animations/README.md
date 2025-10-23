# VRM Animation Assets

This directory contains FBX animation files for the VRM avatar system.

## Animation System

The SyntH animation system automatically manages avatar animations throughout the message processing lifecycle. The system is centrally managed by the `AnimationHandler` in `core/animation_handler.py` and coordinates with the WebUI frontend.

### Animation States

The system defines four logical animation states:

- **Idle**: Default state when no activity is occurring (files: `Idle.fbx`, `Idle2.fbx`, `Happy Idle.fbx`)
- **Think**: Triggered when a message is received (files: `Thinking.fbx`)
- **Write**: Triggered when the LLM starts generating a response (files: `Texting While Standing.fbx`, `Texting.fbx`)
- **Talk**: Can be triggered for speech output (files: `talking.fbx`)

When multiple files are specified for a state, one is randomly selected each time the animation plays.

### Automatic Animation Flow

1. User sends message → **Think** animation plays (looping)
2. LLM starts responding → **Write** animation plays (looping)
3. Response complete → **Idle** animation plays (looping)

No manual intervention is required - animations are automatically triggered by the backend.

For more information, see the [Animation System Documentation](../../docs/animation_system.rst).

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
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig with emphasis on head/upper body

### Response Generation Animations

#### Texting While Standing.fbx
- **Purpose**: Standing typing animation
- **Duration**: Looping
- **Use Case**: Randomly selected when generating responses
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig with emphasis on arms/hands

#### Texting.fbx
- **Purpose**: Seated/casual typing animation
- **Duration**: Looping
- **Use Case**: Randomly selected when generating responses
- **Source**: Mixamo animation library
- **Bones**: Full humanoid rig with emphasis on arms/hands

### Communication Animations

#### talking.fbx
- **Purpose**: Speaking animation
- **Duration**: Looping
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

Edit `core/animation_handler.py` and update the `ANIMATION_MAP`:

```python
ANIMATION_MAP: Dict[AnimationState, List[str]] = {
    AnimationState.THINK: ["Thinking.fbx"],
    AnimationState.WRITE: ["Texting While Standing.fbx", "Texting.fbx"],
    AnimationState.TALK: ["talking.fbx"],
    AnimationState.IDLE: ["Idle.fbx", "Idle2.fbx", "Happy Idle.fbx", "your_animation.fbx"],  # Add here
}
```

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

Edit `core/webui_templates/synth_webui_index.html` and update `animationMappings`:

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
- `gesture_*.fbx` - Hand gestures for emphasis

## License

Animation files from Mixamo are subject to Adobe's Mixamo Terms of Use.
Custom animations should include appropriate licensing information.

## See Also

- [VRM Animation Documentation](../../docs/vrm_animations.rst)
- [Mixamo Animation Library](https://www.mixamo.com)
- [Three.js Animation System](https://threejs.org/docs/#manual/en/introduction/Animation-system)
