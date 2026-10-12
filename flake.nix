{
  # The development shell: kustomize, helm and kubeconform, as scripts/validate-manifests.sh uses.
  #   nix develop -c scripts/validate-manifests.sh
  description = "gitops-cluster";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            kustomize
            kubernetes-helm
            kubeconform
            git
          ];
        };
      });
    };
}
