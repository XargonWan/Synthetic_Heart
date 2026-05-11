import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';
import { FBXLoader } from 'https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/loaders/FBXLoader.js';
import { mixamoVRMRigMap } from '/js/mixamoVRMRigMap.js';

/**
 * Load Mixamo animation, convert for three-vrm use, and return it.
 *
 * @param {string} url A url of mixamo animation data
 * @param {VRM} vrm A target VRM
 * @returns {Promise<THREE.AnimationClip>} The converted AnimationClip
 */
export function loadMixamoAnimation( url, vrm ) {
	const msg = `[loadMixamoAnimation] ⭐ FUNCTION CALLED with url: ${url}, vrm: ${!!vrm}`;
	console.log(msg);
	if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg);
	
	const loader = new FBXLoader(); // A loader which loads FBX
	return loader.loadAsync( url ).then( ( asset ) => {
		try {
			console.log(`[loadMixamoAnimation] ✓ FBX loaded from ${url}, asset.animations.length: ${asset.animations?.length || 0}`);
			let clip = THREE.AnimationClip.findByName( asset.animations, 'mixamo.com' ); // extract the AnimationClip
			
			if (!clip && asset.animations && asset.animations.length > 0) {
				console.warn(`[loadMixamoAnimation] ⚠ 'mixamo.com' not found. Trying first available animation...`);
				console.warn(`[loadMixamoAnimation] Available animations: ${asset.animations.map(a => a.name).join(', ')}`);
				// Fallback: use first animation
				clip = asset.animations[0];
				console.log(`[loadMixamoAnimation] Using fallback animation: ${clip.name}`);
			}
			
			console.log(`[loadMixamoAnimation] Loaded FBX from ${url}, clip name: ${clip?.name}, duration: ${clip?.duration}`);
			console.log(`[loadMixamoAnimation] Clip has ${clip?.tracks?.length || 0} original tracks`);
			console.log(`[loadMixamoAnimation] VRM humanoid available: ${vrm.humanoid ? 'YES' : 'NO'}`);
			// Debug: check if VRM normalized bones are in the scene hierarchy
			if (vrm.humanoid) {
				const hips = vrm.humanoid.getNormalizedBoneNode('hips');
				console.log(`[loadMixamoAnimation] VRM hips node:`, hips ? `FOUND, name="${hips.name}", inScene=${hips.parent ? 'YES' : 'NO'}` : 'MISSING');
				
				// Check if the normalized bone node is actually in vrm.scene
				let foundInScene = false;
				if (hips) {
					vrm.scene.traverse((obj) => {
						if (obj === hips) foundInScene = true;
					});
				}
				console.log(`[loadMixamoAnimation] Hips node in vrm.scene: ${foundInScene ? 'YES' : 'NO'}`);
				
				// Log a few bone names for debugging
				const testBones = ['hips', 'spine', 'chest', 'neck', 'head', 'leftUpperArm', 'rightUpperArm'];
				const boneInfo = [];
				for (const b of testBones) {
					const node = vrm.humanoid.getNormalizedBoneNode(b);
					boneInfo.push(`${b} -> ${node ? `"${node.name}"` : 'MISSING'}`);
				}
				console.log(`[loadMixamoAnimation] VRM bone name mapping:`, boneInfo);
			}
			
			// Debug: show VRM normalized bone names available
			if (vrm.humanoid) {
				const boneNames = [];
				try {
					const rawBones = vrm.humanoid._rawHumanBones?.humanBones || {};
					for (const [key, bone] of Object.entries(rawBones)) {
						if (bone?.node) boneNames.push(`${key} -> "${bone.node.name}"`);
					}
				} catch(e) {}
				console.log(`[loadMixamoAnimation] VRM bone name mapping:`, boneNames.slice(0, 10));
			}
			
			// Debug: show first few Mixamo track names
			if (clip?.tracks?.length > 0) {
				console.log(`[loadMixamoAnimation] First 5 Mixamo track names:`, clip.tracks.slice(0, 5).map(t => t.name));
			}
			
			if (!clip) {
				const msg = `[loadMixamoAnimation] ❌ No animation clip found, available: ${asset.animations?.map(a => a.name).join(', ') || 'NONE'}`;
				console.error(msg);
				if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg, 'error');
				throw new Error('No animation clip found in FBX file');
			}

			const tracks = []; // KeyframeTracks compatible with VRM will be added here

			const restRotationInverse = new THREE.Quaternion();
			const parentRestWorldRotation = new THREE.Quaternion();
			const _quatA = new THREE.Quaternion();
			const _vec3 = new THREE.Vector3();

			// Adjust with reference to hips height.
			const motionHipsHeight = asset.getObjectByName( 'mixamorigHips' ).position.y;
			const vrmHipsY = vrm.humanoid?.getNormalizedBoneNode( 'hips' ).getWorldPosition( _vec3 ).y;
			const vrmRootY = vrm.scene.getWorldPosition( _vec3 ).y;
			const vrmHipsHeight = Math.abs( vrmHipsY - vrmRootY );
			const hipsPositionScale = vrmHipsHeight / motionHipsHeight;
			
			let processedBones = 0;
			let skippedBones = 0;

			clip.tracks.forEach( ( track ) => {

				// Convert each tracks for VRM use, and push to `tracks`
				const trackSplitted = track.name.split( '.' );
				const mixamoRigName = trackSplitted[ 0 ];
				const vrmBoneName = mixamoVRMRigMap[ mixamoRigName ];
				const vrmNode = vrm.humanoid?.getNormalizedBoneNode( vrmBoneName );
				const vrmNodeName = vrmNode?.name;
				const mixamoRigNode = asset.getObjectByName( mixamoRigName );

				if ( vrmNodeName != null ) {
					processedBones++;

					const propertyName = trackSplitted[ 1 ];

					// Store rotations of rest-pose.
					mixamoRigNode.getWorldQuaternion( restRotationInverse ).invert();
					mixamoRigNode.parent.getWorldQuaternion( parentRestWorldRotation );

					if ( track instanceof THREE.QuaternionKeyframeTrack ) {

						// Retarget rotation of mixamoRig to NormalizedBone.
						for ( let i = 0; i < track.values.length; i += 4 ) {

							const flatQuaternion = track.values.slice( i, i + 4 );

							_quatA.fromArray( flatQuaternion );

							// 親のレスト時ワールド回転 * トラックの回転 * レスト時ワールド回転の逆
							_quatA
								.premultiply( parentRestWorldRotation )
								.multiply( restRotationInverse );

							_quatA.toArray( flatQuaternion );

							flatQuaternion.forEach( ( v, index ) => {

								track.values[ index + i ] = v;

							} );

						}

						tracks.push(
							new THREE.QuaternionKeyframeTrack(
								`${vrmNodeName}.${propertyName}`,
								track.times,
								track.values.map( ( v, i ) => ( vrm.meta?.metaVersion === '0' && i % 2 === 0 ? - v : v ) ),
							),
						);

					} else if ( track instanceof THREE.VectorKeyframeTrack ) {

						const value = track.values.map( ( v, i ) => ( vrm.meta?.metaVersion === '0' && i % 3 !== 1 ? - v : v ) * hipsPositionScale );
						tracks.push( new THREE.VectorKeyframeTrack( `${vrmNodeName}.${propertyName}`, track.times, value ) );

					}

				} else {
					skippedBones++;
					console.warn(`[loadMixamoAnimation] WARNING: Could not find VRM bone for Mixamo rig "${mixamoRigName}" (bone name: "${vrmBoneName}")`);
				}

			} );

			console.log(`[loadMixamoAnimation] Converted ${tracks.length} tracks for VRM animation (processed: ${processedBones}, skipped: ${skippedBones})`);
			if (tracks.length > 0) {
				console.log(`[loadMixamoAnimation] First 3 VRM track names:`, tracks.slice(0, 3).map(t => t.name));
				// Check if track node names exist in vrm.scene
				const trackNodeNames = new Set(tracks.slice(0, 5).map(t => t.name.split('.')[0]));
				const sceneNodeNames = new Set();
				vrm.scene.traverse((obj) => sceneNodeNames.add(obj.name));
				const missing = [...trackNodeNames].filter(n => !sceneNodeNames.has(n));
				if (missing.length > 0) {
					console.warn(`[loadMixamoAnimation] WARNING: Track nodes NOT in scene:`, missing);
					console.warn(`[loadMixamoAnimation] Scene node samples:`, [...sceneNodeNames].slice(0, 15));
				} else {
					console.log(`[loadMixamoAnimation] All tested track nodes found in scene`);
				}
			}
			if (tracks.length === 0) {
				console.error(`[loadMixamoAnimation] CRITICAL: Zero tracks converted! VRM will be in T-pose.`);
			}
			// Use a meaningful clip name derived from the source file to avoid multiple
			// clips all being named 'vrmAnimation' which confuses mixer finished events.
			const srcName = (new URL(url, window.location.href).pathname.split('/').pop() || 'vrmAnimation');
			const safeName = decodeURIComponent(srcName);
			const vrmAnimClip = new THREE.AnimationClip( safeName, clip.duration, tracks );
			const msg = `[loadMixamoAnimation] ✓ Created VRM clip: ${vrmAnimClip.tracks.length} tracks, ${vrmAnimClip.duration.toFixed(2)}s`;
			console.log(msg);
			if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg);
			return vrmAnimClip;
		} catch ( err ) {
			console.error(`[loadMixamoAnimation] ERROR converting animation: ${err.message}`, err);
			throw err;
		}

	} ).catch(err => {
		const msg = `[loadMixamoAnimation] ❌ ERROR loading FBX from ${url}: ${err.message}`;
		console.error(msg);
		if (window.SynthWebUISetStatus) window.SynthWebUISetStatus(msg, 'error');
		throw err;
	});

}
