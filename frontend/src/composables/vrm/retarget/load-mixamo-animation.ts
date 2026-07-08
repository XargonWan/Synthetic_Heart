// Ported from res/synth_webui/js/loadMixamoAnimation.js (same repo), which
// derives from the three-vrm humanoidAnimation example. Debug logging removed;
// retargeting math unchanged.
import type { VRM, VRMHumanBoneName } from '@pixiv/three-vrm'

import * as THREE from 'three'
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js'

import { mixamoVRMRigMap } from './mixamo-vrm-rig-map'

function isQuaternionTrackLike(track: THREE.KeyframeTrack, propertyName: string): boolean {
  return (
    track instanceof THREE.QuaternionKeyframeTrack
    || (track as { ValueTypeName?: string }).ValueTypeName === 'quaternion'
    || (propertyName === 'quaternion' && track.getValueSize() === 4)
  )
}

function isPositionTrackLike(track: THREE.KeyframeTrack, propertyName: string): boolean {
  return (
    track instanceof THREE.VectorKeyframeTrack
    || (propertyName === 'position' && (track as { ValueTypeName?: string }).ValueTypeName === 'vector')
    || (propertyName === 'position' && track.getValueSize() === 3)
  )
}

/**
 * Load a Mixamo FBX animation and retarget it onto a VRM's normalized
 * humanoid bones.
 *
 * The returned clip's track names reference `getNormalizedBoneNode()` names —
 * which is why the VRM loader must never merge/rename bones (see loader.ts).
 */
export async function loadMixamoAnimation(url: string, vrm: VRM): Promise<THREE.AnimationClip> {
  const loader = new FBXLoader()
  const asset = await loader.loadAsync(url)

  const clip = THREE.AnimationClip.findByName(asset.animations, 'mixamo.com')
    ?? asset.animations[0]
  if (!clip)
    throw new Error(`No animation clip found in FBX file: ${url}`)

  const tracks: THREE.KeyframeTrack[] = []

  const restRotationInverse = new THREE.Quaternion()
  const parentRestWorldRotation = new THREE.Quaternion()
  const _quatA = new THREE.Quaternion()
  const _vec3 = new THREE.Vector3()

  // Scale hips translation by the ratio of the VRM's hips height to the
  // Mixamo rig's, so root motion matches the model's proportions.
  const motionHips = asset.getObjectByName('mixamorigHips')
  const vrmHipsNode = vrm.humanoid?.getNormalizedBoneNode('hips')
  if (!motionHips || !vrmHipsNode)
    throw new Error(`Missing hips node (fbx: ${!!motionHips}, vrm: ${!!vrmHipsNode}) for ${url}`)
  const motionHipsHeight = motionHips.position.y
  const vrmHipsY = vrmHipsNode.getWorldPosition(_vec3).y
  const vrmRootY = vrm.scene.getWorldPosition(_vec3).y
  const vrmHipsHeight = Math.abs(vrmHipsY - vrmRootY)
  const hipsPositionScale = vrmHipsHeight / motionHipsHeight

  for (const track of clip.tracks) {
    const [mixamoRigName = '', propertyName = ''] = track.name.split('.')
    const vrmBoneName = mixamoVRMRigMap[mixamoRigName]
    const vrmNodeName = vrmBoneName
      ? vrm.humanoid?.getNormalizedBoneNode(vrmBoneName as VRMHumanBoneName)?.name
      : undefined
    const mixamoRigNode = asset.getObjectByName(mixamoRigName)

    if (vrmNodeName == null || !mixamoRigNode)
      continue

    mixamoRigNode.getWorldQuaternion(restRotationInverse).invert()
    mixamoRigNode.parent!.getWorldQuaternion(parentRestWorldRotation)

    if (isQuaternionTrackLike(track, propertyName)) {
      // Retarget rotation of mixamoRig to the normalized bone:
      // parentRestWorldRotation * trackRotation * restRotationInverse
      for (let i = 0; i < track.values.length; i += 4) {
        const flatQuaternion = track.values.slice(i, i + 4)
        _quatA.fromArray(flatQuaternion)
        _quatA.premultiply(parentRestWorldRotation).multiply(restRotationInverse)
        _quatA.toArray(flatQuaternion)
        flatQuaternion.forEach((v, index) => {
          track.values[index + i] = v
        })
      }

      tracks.push(
        new THREE.QuaternionKeyframeTrack(
          `${vrmNodeName}.${propertyName}`,
          track.times as unknown as number[],
          Array.from(track.values).map((v, i) =>
            vrm.meta?.metaVersion === '0' && i % 2 === 0 ? -v : v,
          ),
        ),
      )
    }
    else if (isPositionTrackLike(track, propertyName)) {
      const value = Array.from(track.values).map((v, i) =>
        (vrm.meta?.metaVersion === '0' && i % 3 !== 1 ? -v : v) * hipsPositionScale,
      )
      tracks.push(
        new THREE.VectorKeyframeTrack(
          `${vrmNodeName}.${propertyName}`,
          track.times as unknown as number[],
          value,
        ),
      )
    }
  }

  if (tracks.length === 0)
    throw new Error(`Zero tracks retargeted from ${url} — VRM would stay in T-pose`)

  // Meaningful clip name (from the source filename) so mixer 'finished'
  // events can be told apart.
  const srcName = new URL(url, window.location.href).pathname.split('/').pop() || 'vrmAnimation'
  return new THREE.AnimationClip(decodeURIComponent(srcName), clip.duration, tracks)
}
