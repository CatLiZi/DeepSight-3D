"""Render repository Blender models without saving or altering the source files."""
import bpy, sys
from mathutils import Vector
from pathlib import Path

args=sys.argv[sys.argv.index('--')+1:]
head,body,out=(Path(value).resolve() for value in args)
out.mkdir(parents=True,exist_ok=True)

def open_head():
    bpy.ops.wm.open_mainfile(filepath=str(head),load_ui=False,use_scripts=False)
    for obj in bpy.context.scene.objects:
        if obj.type in {'MESH','CURVE'} and obj.hide_get():obj.hide_render=True

def append_body():
    with bpy.data.libraries.load(str(body),link=False) as (data_from,data_to):
        data_to.objects=[name for name in data_from.objects if not name.startswith(('PRINT_', 'HIDDEN_'))]
    for obj in data_to.objects:
        if obj is not None and not obj.hide_render:
            bpy.context.scene.collection.objects.link(obj)

def studio(name,camera_pos,target,scale):
    scene=bpy.context.scene
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=20
    scene.render.resolution_x=1400;scene.render.resolution_y=1120;scene.render.resolution_percentage=100
    scene.render.image_settings.file_format='PNG';scene.render.film_transparent=False
    scene.world=bpy.data.worlds.new('README studio world');scene.world.use_nodes=True
    bg=scene.world.node_tree.nodes.get('Background');bg.inputs['Color'].default_value=(.70,.74,.78,1);bg.inputs['Strength'].default_value=.45
    camera_data=bpy.data.cameras.new('README camera');camera=bpy.data.objects.new('README camera',camera_data);scene.collection.objects.link(camera)
    camera.location=camera_pos;camera.rotation_euler=(Vector(target)-camera.location).to_track_quat('-Z','Y').to_euler()
    camera_data.type='ORTHO';camera_data.ortho_scale=scale;scene.camera=camera
    for i,(location,power,size) in enumerate([((-.45,-.6,.8),55.,.7),((.6,-.2,.55),40.,.6),((.0,.55,.65),75.,.5)]):
        light_data=bpy.data.lights.new('README light '+str(i),'AREA');light_data.energy=power;light_data.shape='DISK';light_data.size=size
        light=bpy.data.objects.new('README light '+str(i),light_data);scene.collection.objects.link(light);light.location=location
        light.rotation_euler=(Vector(target)-light.location).to_track_quat('-Z','Y').to_euler()
    scene.view_settings.view_transform='AgX'
    scene.render.filepath=str(out/name)
    bpy.ops.render.render(write_still=True)

open_head();append_body()
studio('panda-assembled-cad.png',(.42,-.8,.40),(0.,-.005,.145),.49)

open_head()
for obj in bpy.context.scene.objects:
    if obj.name.startswith('V17_02_head_rear_original_ear'):obj.hide_render=True
    if obj.name.startswith('WIRE_'):
        obj.hide_set(False);obj.hide_render=False
studio('head-interior-cad.png',(.26,.58,.34),(0.,-.005,.205),.33)

bpy.ops.wm.open_mainfile(filepath=str(body),load_ui=False,use_scripts=False)
for obj in bpy.context.scene.objects:
    if obj.type in {'MESH','CURVE'} and (obj.hide_get() or obj.name.startswith('V16_08_body_rear_snap')):obj.hide_render=True
    if obj.name.startswith('WIRE_V16_fixed_neck'):
        obj.hide_set(False);obj.hide_render=False
studio('body-interior-cad.png',(.24,.55,.25),(0.,0.,.07),.29)
