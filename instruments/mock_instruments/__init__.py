"""The mock instruments' behaviour, independent of the protocol that serves it.

Each module here is one instrument: its internal state, the rules its commands enforce, how long
they take, how its status moves, and what it does to the shared world model. The SiLA2 servers
(`sila2/servers/`) and the LADS OPC UA servers (`lads/servers/`) are thin adapters over these
classes, so the two protocols cannot drift apart in behaviour -- there is only one copy of it.

    centrifuge      Centrifuge       MicroplateCentrifugeController
    plateloc        PlateLoc         PlateLocController
    seal_remover    SealRemover      AutomatedPlateSealRemoverController
    thermal_cycler  ThermalCycler    AutomatedThermalCyclerController
    ardea           Ardea            the transporter (LabwareService / CarriageService)

`runtime` and `errors` are the protocol-neutral plumbing they share. What the world *means*
deliberately stays in each instrument module rather than here (`docs/RULES.md`).

This is a local package of this repository, installed into each server image; it is not
published anywhere.
"""
