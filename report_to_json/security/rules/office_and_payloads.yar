/*
  Office (OOXML) active content and generic executable payloads.

  OOXML files are ZIP containers, so the gate scans every archive member
  individually. External variable `kind` says what is being scanned:
    "raw"        the input file as-is
    "ooxml-part" a document part (XML, media) from an OOXML container
    "embedded"   an embedded object / attachment (OLE object, PDF attachment)
  Security reports routinely quote payload strings in their text, so string
  heuristics only run on embedded objects, never on document text.

  meta.verdict: "malicious" | "suspicious" | "test"
*/

rule Office_VBA_Project
{
    meta:
        verdict = "suspicious"
        description = "VBA macro project"
    strings:
        $ole = { D0 CF 11 E0 A1 B1 1A E1 }
        $vba1 = "_VBA_PROJECT" wide ascii
        $vba2 = "Attribute VB_Name" ascii nocase
        $vba3 = "VBAProject" ascii
    condition:
        (kind == "ooxml-part" or kind == "embedded") and $ole at 0 and any of ($vba*)
}

rule Office_AutoExec_Macro
{
    meta:
        verdict = "malicious"
        description = "Macro with an auto-execute entry point"
    strings:
        $ole = { D0 CF 11 E0 A1 B1 1A E1 }
        $vba = "Attribute VB_" ascii nocase
        $a1 = "AutoOpen" ascii nocase
        $a2 = "Document_Open" ascii nocase
        $a3 = "Workbook_Open" ascii nocase
        $a4 = "AutoExec" ascii nocase
    condition:
        (kind == "ooxml-part" or kind == "embedded") and $ole at 0 and $vba and any of ($a*)
}

rule Office_DDE_Field
{
    meta:
        verdict = "malicious"
        description = "DDE/DDEAUTO field that can run commands when the document opens"
    strings:
        $dde = /instrText[^>]*>\s*DDE(AUTO)?\b/ nocase
        $ddefld = /fldSimple[^>]+instr="\s*DDE(AUTO)?\b/ nocase
    condition:
        kind == "ooxml-part" and any of them
}

rule Executable_Payload
{
    meta:
        verdict = "malicious"
        description = "Windows PE or ELF executable"
    strings:
        $mz = { 4D 5A }
        $pe = { 50 45 00 00 }
        $elf = { 7F 45 4C 46 }
    condition:
        ($mz at 0 and $pe in (0x40..0x400)) or $elf at 0
}

rule Script_Dropper_Strings
{
    meta:
        verdict = "suspicious"
        description = "Command/script execution strings typical of droppers"
    strings:
        $s1 = "powershell -e" ascii nocase
        $s2 = "powershell.exe -enc" ascii nocase
        $s3 = "WScript.Shell" ascii nocase
        $s4 = "cmd.exe /c" ascii nocase
        $s5 = "mshta " ascii nocase
        $s6 = "certutil -decode" ascii nocase
    condition:
        kind == "embedded" and any of them
}

rule EICAR_Test_File
{
    meta:
        verdict = "test"
        description = "EICAR anti-virus test signature"
    strings:
        // Split so this rule file is not itself flagged by anti-virus scanners.
        $a = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$"
        $b = "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    condition:
        $a and $b
}
