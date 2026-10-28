param([string]$Output)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
[ComImport, Guid("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")] class FocusDeviceEnum {}
[ComImport, Guid("29840822-5B84-11D0-BD3B-00A0C911CE86"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IFocusCreateDevEnum { [PreserveSig] int CreateClassEnumerator(ref Guid category, out IEnumMoniker result, int flags); }
[ComImport, Guid("C6E13370-30AC-11D0-A18C-00A0C9118956"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IFocusCameraControl {
 [PreserveSig] int GetRange(int p,out int min,out int max,out int step,out int def,out int caps);
 [PreserveSig] int Set(int p,int value,int flags);
 [PreserveSig] int Get(int p,out int value,out int flags);
}
[ComImport, Guid("C6E13360-30AC-11D0-A18C-00A0C9118956"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IFocusVideoProcAmp {
 [PreserveSig] int GetRange(int p,out int min,out int max,out int step,out int def,out int caps);
 [PreserveSig] int Set(int p,int value,int flags);
 [PreserveSig] int Get(int p,out int value,out int flags);
}
public class FocusCapabilityProbe {
 public static object Read() {
  var enumerator=(IFocusCreateDevEnum)new FocusDeviceEnum();
  Guid category=new Guid("860BB310-5D01-11D0-BD3B-00A0C911CE86"); IEnumMoniker list;
  if(enumerator.CreateClassEnumerator(ref category,out list,0)!=0)throw new Exception("No video devices");
  IMoniker[] item=new IMoniker[1];
  while(list.Next(1,item,IntPtr.Zero)==0) {
   string path;item[0].GetDisplayName(null,null,out path);
   if(!path.ToLowerInvariant().Contains("vid_1bcf&pid_0b15"))continue;
   object filter;Guid iid=new Guid("56A86895-0AD4-11CE-B03A-0020AF0BA770");item[0].BindToObject(null,null,ref iid,out filter);
   var rows=new List<object>();
   foreach(var pair in new[]{Tuple.Create("Focus",6),Tuple.Create("Exposure",4),Tuple.Create("Zoom",3)}) {
    int min=0,max=0,step=0,def=0,caps=0,value=0,flags=0,hr=0,ghr=0;
    try{var c=(IFocusCameraControl)filter;hr=c.GetRange(pair.Item2,out min,out max,out step,out def,out caps);ghr=c.Get(pair.Item2,out value,out flags);}catch(Exception e){hr=Marshal.GetHRForException(e);ghr=hr;}
    rows.Add(new{property=pair.Item1,group="CameraControl",range_hr=hr,get_hr=ghr,supported=hr>=0,min,max,step,default_value=def,capabilities=caps,auto_capable=(caps&1)!=0,manual_capable=(caps&2)!=0,value,flags});
   }
   foreach(var pair in new[]{Tuple.Create("Brightness",0),Tuple.Create("Contrast",1),Tuple.Create("Sharpness",4),Tuple.Create("WhiteBalance",7),Tuple.Create("Gain",9)}) {
    int min=0,max=0,step=0,def=0,caps=0,value=0,flags=0,hr=0,ghr=0;
    try{var c=(IFocusVideoProcAmp)filter;hr=c.GetRange(pair.Item2,out min,out max,out step,out def,out caps);ghr=c.Get(pair.Item2,out value,out flags);}catch(Exception e){hr=Marshal.GetHRForException(e);ghr=hr;}
    rows.Add(new{property=pair.Item1,group="VideoProcAmp",range_hr=hr,get_hr=ghr,supported=hr>=0,min,max,step,default_value=def,capabilities=caps,auto_capable=(caps&1)!=0,manual_capable=(caps&2)!=0,value,flags});
   }
   Marshal.ReleaseComObject(filter);
   return new{device_path=path,controls=rows,hardware_writes=false};
  }
  throw new Exception("Camera 1BCF:0B15 not found");
 }
}
'@
$result = [FocusCapabilityProbe]::Read() | ConvertTo-Json -Depth 6
if ($Output) { [System.IO.File]::WriteAllText($Output, $result, [System.Text.Encoding]::UTF8) }
$result
