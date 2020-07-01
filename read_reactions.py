import io_utils
import analysis
from analysis import *


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Read reaction products."
        )
    parser.add_argument("folder", help="Folder containing the react*** files.")
    parser.add_argument("-c", "--resolve-chiral",
                        help="Resolve chiral compounds. Defaults to no.",
                        action="store_true")
    parser.add_argument("--all", help="Do not limit reaction network to reactant"
                        +" and connected products. Defaults to false.",
                        action="store_true")
    parser.add_argument("--ts", help="Sort products by TS energy as opposed"
                        +" to enthalpy. (the default).",
                        action="store_true")
    parser.add_argument("--local", help="Use reaction-local barrier instead of TS energy.",
                        action="store_true")
    parser.add_argument("-r", "--redo", help="Re-read the reaction data without using the"
                        + " cache. This is useful when changing parser.",
                        action="store_true")
    parser.add_argument("-e", "--exclude", help="Parse reaction data excluding certain atoms. Arguments should be the excluded atom indices.", type=int, nargs='+')
    args = parser.parse_args()
    folder =args.folder
    if isinstance(args.exclude, list):
        exclude = set(args.exclude)
        print("Excluding atoms: ", exclude)
    else:
        exclude = []

    if len(exclude):
        chemical_id = "reparsed"
        reactant, E = io_utils.traj2smiles(folder + "/init_opt.xyz",
                                           index=0, chiral=args.resolve_chiral,
                                           exclude=exclude)

        # make a parser that exclude certain atoms
        def reparser(filepath):
            smiles, energy = io_utils.traj2smiles(filepath,
                                                  index=0, chiral=args.resolve_chiral,
                                                  exclude=exclude)
            return smiles

    elif args.resolve_chiral:
        chemical_id = "SMILES_c"
        reactant, E = io_utils.traj2smiles(folder + "/init_opt.xyz",
                                           index=0,chiral=True)
        reparser = None
    else:
        chemical_id = "SMILES_i"
        reactant, E = io_utils.traj2smiles(folder + "/init_opt.xyz",
                                           index=0,chiral=False)
        reparser = None

    pathways = read_all_reactions(folder, restart=(not args.redo), reparser=reparser)
    species = get_species_table(pathways, chemical_id=chemical_id)
    species.to_csv(folder + "/parsed_species.csv")

    if args.all:
        final = analyse_reaction_network(pathways,species,list(species.index)[::-1],
                                         sort_by_barrier=args.ts,
                                         reaction_local=args.local,
                                         chemical_id=chemical_id)
    else:
        print("Reactant: %s" % reactant)
        if reactant in species.index:
            final = analyse_reaction_network(pathways,species,[reactant],
                                             sort_by_barrier=args.ts,
                                             reaction_local=args.local,
                                             chemical_id=chemical_id)
        else:
            print("Error! Reactant not in found species")
            raise SystemExit(-1)

    # Finally, save parsed reaction network
    final.to_csv(folder + "/parsed_reactions.csv")
