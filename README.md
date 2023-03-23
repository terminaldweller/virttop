# virttop
a top like utility for libvirt


![Image](virttop.png)

## How to get
```sh
pip install virttop
```

## Configfile
The default location for the config file is '~/.virttop.toml'.

```toml
[color]
name_column_fg=23
name_column_bg=0
active_row_fg=24
active_row_bg=0
inactive_row_fg=244
inactive_row_bg=0
box_fg=29
box_bg=0
selected_fg=0
selected_bg=36
```

## Keybindings

`j` and `k` and arrow keys move up and down.</br>
`g` moves to the top of the list.</br>
`G` moved to the bottom of the list.</br>
`r` runs an inactive domain.</br>
`s` shuts down a running domain.</br>
`d` destroy a running domain.</br>
