param([Nullable[int]]$Exposure, [Nullable[int]]$Flags)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
[ComImport, Guid("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")]
class SystemDeviceEnum {}
[ComImport, Guid("29840822-5B84-11D0-BD3B-00A0C911CE86"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ICreateDevEnum {
  [PreserveSig] int CreateClassEnumerator(ref Guid category, out IEnumMoniker result, int flags);
}
[ComImport, Guid("C6E13370-30AC-11D0-A18C-00A0C9118956"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAMCameraControl {
  [PreserveSig] int GetRange(int property, out int min, out int max, out int step, out int def, out int caps);
  [PreserveSig] int Set(int property, int value, int flags);
  [PreserveSig] int Get(int property, out int value, out int flags);
}
public class StereoCameraControls {
  private IAMCameraControl control;
  public StereoCameraControls() {
    var enumerator = (ICreateDevEnum)new SystemDeviceEnum();
    Guid category = new Guid("860BB310-5D01-11D0-BD3B-00A0C911CE86");
    IEnumMoniker list;
    if(enumerator.CreateClassEnumerator(ref category, out list, 0) != 0) throw new Exception("No camera monikers");
    IMoniker[] item = new IMoniker[1];
    while(list.Next(1, item, IntPtr.Zero) == 0) {
      string path; item[0].GetDisplayName(null, null, out path);
      if(path.ToLowerInvariant().Contains("vid_1bcf&pid_0b15")) {
        object filter; Guid iid = new Guid("56A86895-0AD4-11CE-B03A-0020AF0BA770");
        item[0].BindToObject(null, null, ref iid, out filter);
        control = (IAMCameraControl)filter; return;
      }
    }
    throw new Exception("Selected USB camera not found");
  }
  public int[] Read() {
    int min,max,step,def,caps,value,flags;
    Marshal.ThrowExceptionForHR(control.GetRange(4,out min,out max,out step,out def,out caps));
    Marshal.ThrowExceptionForHR(control.Get(4,out value,out flags));
    return new int[] {min,max,step,def,caps,value,flags};
  }
  public void Set(int value, int flags) { Marshal.ThrowExceptionForHR(control.Set(4,value,flags)); }
}
'@
$cameraControl = New-Object StereoCameraControls
$before = $cameraControl.Read()
if ($null -ne $Exposure) {
    $exposureFlags = if ($null -ne $Flags) { [int]$Flags } else { $before[6] }
    $cameraControl.Set([int]$Exposure, $exposureFlags)
}
[PSCustomObject]@{Min=$before[0];Max=$before[1];Step=$before[2];Default=$before[3];Capabilities=$before[4];BeforeValue=$before[5];BeforeFlags=$before[6];After=$cameraControl.Read()} | ConvertTo-Json -Compress
