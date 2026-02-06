# WebUI JavaScript Modules

This directory contains ES6 modules for the SyntH WebUI VRM avatar system.

## Module Files

### mixamoVRMRigMap.js
**Purpose**: Bone name mapping from Mixamo skeleton to VRM humanoid bones

**Exports**:
- `mixamoVRMRigMap`: Object mapping Mixamo bone names to VRM bone names

**Example**:
```javascript
{
  mixamorigHips: 'hips',
  mixamorigSpine: 'spine',
  mixamorigLeftArm: 'leftUpperArm',
  // ... etc
}
```

**Usage**:
```javascript
import { mixamoVRMRigMap } from './mixamoVRMRigMap.js';
const vrmBoneName = mixamoVRMRigMap['mixamorigHead']; // 'head'
```

### loadMixamoAnimation.js
**Purpose**: Converts Mixamo FBX animations to VRM-compatible animation clips

**Exports**:
- `loadMixamoAnimation(url, vrm)`: Async function that loads and converts animations

**Algorithm**:
1. Load FBX file using FBXLoader
2. Extract 'mixamo.com' animation clip
3. Calculate hip height scaling between Mixamo and VRM models
4. Retarget each bone track:
   - Quaternion rotations: Apply parent world rotation and rest pose inverse
   - Vector positions: Scale by hip height ratio
5. Create new AnimationClip with retargeted tracks

**Parameters**:
- `url` (string): Path to FBX animation file
- `vrm` (VRM): Target VRM model instance

**Returns**:
- `Promise<THREE.AnimationClip>`: Converted animation clip ready for AnimationMixer

**Example**:
```javascript
import { loadMixamoAnimation } from './loadMixamoAnimation.js';

const clip = await loadMixamoAnimation('/static/animations/idle.fbx', vrm);
const action = mixer.clipAction(clip);
action.play();
```

## Integration with WebUI

These modules are imported via `core/webui_templates/synth_webui_shell.html` and the section templates:

```javascript
import { loadMixamoAnimation } from '/static/js/loadMixamoAnimation.js';
import { mixamoVRMRigMap } from '/static/js/mixamoVRMRigMap.js';
```

The import map in the HTML ensures Three.js and addons are resolved correctly (see `core/webui_templates/synth_webui_shell.html`):

## Bootstrap helper

`webui-bootstrap.js` provides a lightweight runtime stub for the animation handler and VRM loader path configuration until `vrm-viewer.mjs` has finished loading.

```html
<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.169.0/examples/jsm/"
  }
}
</script>
```

## Technical Details

### Bone Retargeting

The retargeting process handles differences between Mixamo and VRM skeletons:

1. **Rest Pose Storage**: Capture Mixamo bone rest rotations
2. **Parent World Rotation**: Account for hierarchy differences
3. **Quaternion Adjustment**: `parentWorldRot * trackRot * restRotInverse`
4. **VRM v0 Compatibility**: Flip X/Z values for older VRM spec

### Hip Height Scaling

Critical for proper animation scaling:

```javascript
const motionHipsHeight = mixamoAsset.getObjectByName('mixamorigHips').position.y;
const vrmHipsHeight = vrm.humanoid.getNormalizedBoneNode('hips').getWorldPosition().y;
const scale = vrmHipsHeight / motionHipsHeight;
```

This ensures animations look natural regardless of VRM model size.

### VRM Meta Version Handling

```javascript
// VRM v0 requires value negation for certain axes
tracks.push(
  new THREE.QuaternionKeyframeTrack(
    `${vrmNodeName}.quaternion`,
    track.times,
    track.values.map((v, i) => 
      (vrm.meta?.metaVersion === '0' && i % 2 === 0) ? -v : v
    )
  )
);
```

## Adding New Utility Modules

To add new JavaScript utilities:

1. **Create Module File**:
   ```javascript
   // myUtility.js
   export function myFunction(param) {
     // Implementation
   }
   ```

2. **Import in HTML**:
   ```javascript
   import { myFunction } from '/static/js/myUtility.js';
   ```

3. **Use in Code**:
   ```javascript
   const result = myFunction(data);
   ```

## Dependencies

### External Libraries
- **Three.js r169**: Core 3D library
- **@pixiv/three-vrm v3**: VRM model loader and utilities
- **FBXLoader**: Part of Three.js examples/jsm/loaders

### Browser Requirements
- ES6 Module support
- WebGL 2.0
- Modern JavaScript features (async/await, destructuring, etc.)

## Debugging

### Module Loading Issues

**Check Browser Console**:
```javascript
// Failed to load module: ERR_MODULE_NOT_FOUND
// Solution: Verify file exists and path is correct
```

### Animation Conversion Errors

**Check Bone Mapping**:
```javascript
console.log('VRM bones:', vrm.humanoid.humanBones);
console.log('Mixamo bones:', mixamoAsset.children);
```

### Performance Profiling

```javascript
console.time('Animation Load');
const clip = await loadMixamoAnimation(url, vrm);
console.timeEnd('Animation Load');
// Typical time: 100-500ms for 5MB FBX file
```

## File Locations

```
res/synth_webui/js/
├── mixamoVRMRigMap.js          # Bone mapping definitions
├── loadMixamoAnimation.js      # Animation conversion utility
└── README.md                   # This file
```

## See Also

- [VRM Animation Documentation](../../docs/vrm_animations.rst)
- [Three.js Animation System](https://threejs.org/docs/#manual/en/introduction/Animation-system)
- [VRM Specification](https://github.com/vrm-c/vrm-specification)
- [Mixamo Character System](https://www.mixamo.com)
